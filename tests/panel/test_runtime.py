"""Внешний исполнитель вынесен из потока SQLite; пауза проходит во время ответа."""
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

from iam.backup import create_backup, default_backup_dir
from iam.domain import new_id
from iam.panel.runtime import Runtime
from iam.providers.fake import FakeClaude, auth_error, garbage, reply
from iam.session import SessionConflict
from iam.state import EventSpec, StateController, verify
from iam.storage import AlreadyLocked, Store


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="iam-runtime-"))
        self.addCleanup(shutil.rmtree,self.tmp,ignore_errors=True)
        self.db=self.tmp/"test.db"

    def runtime(self, **kwargs):
        rt=Runtime(self.db,interval=0.01,**kwargs)
        rt.start()
        self.addCleanup(rt.close)
        return rt

    def seed(self, persona=None):
        with Store.create(self.db,persona or new_id("exp")) as s:
            StateController(s).create_persona()

    def test_only_one_writer_and_clean_shutdown_pause(self):
        rt=self.runtime(demo=True)
        with self.assertRaises(AlreadyLocked): Store.open(self.db)
        rt.submit("command","start","тест").result(5)
        cancelled=Future();cancelled.cancel()
        rt.jobs.put((cancelled,"view",(),{}))
        rt.submit("view").result(5)
        rt.close()
        self.assertFalse(rt.thread.is_alive())
        self.assertTrue(rt.closed)
        with self.assertRaises(RuntimeError): rt.submit("view").result(1)
        with Store.open(self.db) as s:
            self.assertEqual(verify(s),[])
            self.assertEqual(s.latest_snapshot().state["world_time"]["session_state"],"paused")

    def test_setup_failure_does_not_leak_writer_lock(self):
        self.seed(new_id("per"))
        rt=Runtime(self.db,demo=True)
        with self.assertRaises(ValueError): rt.start()
        rt.close()
        with Store.open(self.db) as s: self.assertEqual(verify(s),[])

    def test_fatal_step_drains_pending_jobs_and_closes_database(self):
        rt=self.runtime(demo=True)
        failed=threading.Event()
        def fail(*args):
            failed.set()
            raise OSError("диск")
        rt._fail = lambda: setattr(rt.session,"step",fail)
        rt.submit("_fail").result(5)
        self.assertTrue(failed.wait(5))
        rt.thread.join(5)
        self.assertTrue(rt.closed)
        self.assertEqual(rt.fatal,"OSError")
        with self.assertRaises(RuntimeError): rt.submit("view").result(1)

    def test_live_cycle_pauses_while_model_waits_and_late_response_is_stale(self):
        self.seed()
        started,release=threading.Event(),threading.Event()
        def delayed(*args):
            started.set()
            if not release.wait(5): raise TimeoutError("test release")
            return reply([])
        with mock.patch("iam.panel.runtime.subprocess_runner",side_effect=delayed):
            rt=self.runtime(token_file="/tmp/iam-test-token.env")
            try:
                rt.submit("command","start","запуск").result(5)
                rt.inject=lambda: rt.ctl.record_event(EventSpec("input.arrived","rx:test:1",{"text":"привет"}))
                rt.submit("inject").result(5)
                self.assertTrue(started.wait(5))
                rt.submit("command","pause","ручная пауза").result(2)
                view=rt.submit("view").result(2)
                self.assertTrue(view["usage"]["in_flight"])
                with self.assertRaises(SessionConflict): rt.submit("command","resume","рано").result(2)
                self.assertEqual(view["session_state"],"paused")
                self.assertEqual(view["usage"]["calls"],1)
            finally:
                release.set()
            def wait_for_response():
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    v=rt.submit("view").result(2)
                    if any(e["type"]=="proposal.stale" for e in v["events"]): return v
                self.fail("Поздний ответ не отмечен как устаревший")
            view=wait_for_response()
            self.assertEqual(view["session_state"],"paused")
            rt.check=lambda: verify(rt.store)
            self.assertEqual(rt.submit("check").result(5),[])

    def test_format_repair_counts_two_real_executions_and_resume_keeps_budget(self):
        self.seed()
        fake=FakeClaude([garbage(),reply([])])
        with mock.patch("iam.panel.runtime.subprocess_runner",fake):
            rt=self.runtime(token_file="/tmp/iam-test-token.env")
            rt.submit("command","start","формат",max_calls=2).result(5)
            rt.inject=lambda: rt.ctl.record_event(EventSpec("input.arrived","rx:test:1",{"text":"test"}))
            rt.submit("inject").result(5)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                view=rt.submit("view").result(2)
                if view["session_state"]=="paused":break
            self.assertEqual(view["usage"]["calls"],2)
            self.assertEqual(view["pause_reason"],"session_limit")
            self.assertEqual(len(fake.calls),2)
            self.assertTrue(any(e["type"]=="proposal.received" for e in view["events"]))

    def test_auth_error_uses_technical_pause(self):
        self.seed()
        with mock.patch("iam.panel.runtime.subprocess_runner",FakeClaude([auth_error()])):
            rt=self.runtime(token_file="/tmp/iam-test-token.env")
            rt.submit("command","start","авторизация").result(5)
            rt.inject=lambda: rt.ctl.record_event(EventSpec("input.arrived","rx:test:1",{"text":"test"}))
            rt.submit("inject").result(5)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                view=rt.submit("view").result(2)
                if view["session_state"]=="paused":break
            self.assertEqual(view["pause_reason"],"auth_lost")

    def test_telegram_wiring_and_poll_freshness_without_external_network(self):
        self.seed()
        client=mock.Mock(bot_id=123,timeout=0)
        client.call.return_value=[]
        original_call=client.call
        with mock.patch("iam.panel.runtime.Client",return_value=client):
            rt=self.runtime(token_file="/tmp/iam-test-token.env",telegram_user=123)
            rt.poll=lambda: rt.receiver.poll_once()
            rt.submit("poll").result(5)
            view=rt.submit("view").result(5)
            self.assertEqual(view["capabilities"][0]["name"],"Telegram")
            self.assertIsNotNone(view["last_poll"])
            self.assertFalse(view["poll_stale"])
            original_call.assert_called()
