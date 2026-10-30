import json
from unittest.mock import patch

from iam.cycle import Cycle, Runner
from iam.cycle.runner import input_id_for
from iam.domain import Op
from iam.state import EventSpec, RecordResult
from iam.stubs import Reply, ScriptedProvider, StubCapabilities, StubCreatorBridge, StubMemory, StubWorld, rest
from iam.telegram import CreatorBridge, Receiver, TelegramError

from .helpers import TelegramCase, USER, update


class ReceiverTest(TelegramCase):
    def arrivals(self):
        return [e for e in self.c.store.events() if e.type == "input.arrived"]

    def test_pause_durable_inbox_then_ack_restart_no_duplicate(self):
        self.assertTrue(self.c.start_session().applied)
        self.assertTrue(self.c.pause_session().applied)
        self.assertEqual(self.c.session_state, "paused")
        self.assertIsNone(self.rx.last_poll_at)
        self.api.updates = [update()]
        version, life = self.c.state_version, self.c.life_time_ms
        self.assertEqual(self.rx.poll_once(), 1)
        self.assertEqual(self.api.calls[0][1], {"offset": 0, "timeout": 0, "limit": 100,
                                              "allowed_updates": ["message"]})
        self.assertEqual(len(self.api.updates), 1)  # ещё не подтверждено следующим getUpdates
        iid = input_id_for("telegram", "10")
        self.assertEqual(self.c.store.get_inbox(iid).content, {"text": "привет"})
        self.assertEqual((self.c.state_version, self.c.life_time_ms), (version, life))
        self.c = self.reopen(self.c)
        self.rx = Receiver(self.c, self.client, [USER])
        self.assertEqual(self.rx.offset, 11)
        self.assertIsNotNone(self.rx.last_poll_at)
        self.assertEqual(self.rx.poll_once(), 0)
        self.assertEqual(self.api.updates, [])
        self.assertEqual(len(self.arrivals()), 1)
        self.assertEqual(self.c.store.integrity_problems(), [])

    def test_storage_failure_rolls_back_event_inbox_and_offset(self):
        self.api.updates = [update()]
        original = self.c.store.add_inbox
        def crash(item):
            original(item)
            raise OSError("disk full")
        with patch.object(self.c.store, "add_inbox", crash), self.assertRaises(OSError):
            self.rx.poll_once()
        self.assertEqual(self.rx.offset, 0)
        self.assertEqual(self.arrivals(), [])
        self.assertIsNone(self.c.store.find_inbox("telegram", "10"))
        self.assertEqual(self.rx.poll_once(), 1)
        self.assertEqual(self.rx.poll_once(), 0)

    def test_partial_batch_crash_preserves_completed_updates(self):
        self.api.updates = [update(11), update(10)]
        original = self.c.store.add_inbox
        def crash(item):
            if item.external_id == "11":
                raise OSError("disk full")
            return original(item)
        with patch.object(self.c.store, "add_inbox", crash), self.assertRaises(OSError):
            self.rx.poll_once()
        self.assertEqual(self.rx.offset, 11)
        self.assertEqual(len(self.arrivals()), 1)
        self.assertEqual(self.rx.poll_once(), 1)
        self.assertEqual(len(self.arrivals()), 2)

    def test_duplicates_identical_text_distinct_updates(self):
        self.api.updates = [update(), update(), update(11)]
        self.assertEqual(self.rx.poll_once(), 2)
        self.assertEqual(self.rx.notices(), {"count": 2})

    def test_filter_unknown_group_username_bot_and_unsupported(self):
        bad = [update(10, sender=88), update(11, kind="group"), update(12, sender="operator"),
               update(13, chat=-USER), {"update_id": 14}, update(15)]
        bad[-1]["message"]["from"]["is_bot"] = True
        for i, value in enumerate((None, [], {"from": [], "chat": []}, {"text": 44}), 16):
            bad.append({"update_id": i, "message": value})
        bad.append(update(20, sender=True, chat=True))
        self.api.updates = bad
        self.assertEqual(self.rx.poll_once(), 0)
        self.assertEqual(self.rx.offset, 21)
        self.assertEqual(self.rx.notices(), {"count": 0})
        self.assertEqual(len([e for e in self.c.store.events() if e.type == "system.rejected_sender"]), len(bad))
        self.assertNotIn("привет", json.dumps([e.to_dict() for e in self.c.store.events()], ensure_ascii=False))

    def test_network_and_malformed_updates_do_not_ack(self):
        self.api.mode = "drop"
        with self.assertRaises(TelegramError):
            self.rx.poll_once()
        for value in ({}, [None], [{"update_id": True}], [{"update_id": -1}], [{"update_id": "10"}]):
            self.api.mode, self.api.result = "ok", value
            with self.assertRaises(TelegramError):
                self.rx.poll_once()
        self.assertEqual(self.rx.offset, 0)
        self.assertIsNone(self.rx.last_poll_at)

    def test_controller_reject_does_not_ack(self):
        self.api.updates = [update()]
        with patch.object(self.c, "record_event", return_value=RecordResult("rejected")):
            with self.assertRaises(RuntimeError):
                self.rx.poll_once()
        self.assertEqual(self.rx.offset, 0)

    def test_notices_hide_body_until_read_event(self):
        self.api.updates = [update(text="секретное содержание")]
        self.rx.poll_once()
        iid = input_id_for("telegram", "10")
        self.assertEqual(self.rx.notices(), {"count": 1})
        n2 = self.rx.notices("N2")
        self.assertEqual(n2["items"][0]["sender"], str(USER))
        self.assertNotIn("секретное", json.dumps(n2, ensure_ascii=False))
        with self.assertRaises(ValueError):
            self.rx.notices("N3")
        with self.assertRaises(PermissionError):
            self.rx.read_content(iid)
        self.c.start_session()
        cause = self.arrivals()[0].event_id
        noticed = self.c.apply(self.prop(self.c, Op("preferences", "notice", "set", True, True), cause=cause,
            carrier=EventSpec("input.noticed", f"ntc:{iid}:N1", {"input_id": iid, "notice_level": "N1"})))
        self.assertTrue(noticed.applied)
        read = self.c.apply(self.prop(self.c, Op("preferences", "read", "set", True, True),
            cause=noticed.events[0].event_id, carrier=EventSpec("input.read", f"rd:{iid}", {"input_id": iid})))
        self.assertTrue(read.applied)
        self.assertEqual(self.rx.read_content(iid), {"text": "секретное содержание"})
        self.assertEqual(self.rx.notices("N2"), {"count": 0, "items": []})

    def test_runner_paused_no_model_then_notice_and_no_rights_command(self):
        body = "/grant admin все технические права"
        self.api.updates = [update(text=body)]
        provider = ScriptedProvider([Reply((rest(),))])
        bridge = CreatorBridge(self.rx, self.tx, USER)
        cycle = Cycle(self.c, provider, StubMemory(), StubWorld(), StubCapabilities(), bridge)
        runner = Runner(cycle, self.c, bridge)
        before = cycle.capabilities.list()
        self.assertEqual(runner.run_pending(), [])
        self.assertEqual(provider.calls, [])
        self.c.start_session()
        self.assertEqual(len(runner.run_pending()), 1)
        self.assertEqual(len(provider.calls), 1)
        self.assertNotIn(body, json.dumps(provider.calls, ensure_ascii=False))
        self.assertEqual(cycle.capabilities.list(), before)
        self.assertNotIn("operator.intervention", self.types(self.c))
        self.assertEqual(len(self.arrivals()), 1)

    def test_telegram_network_failure_does_not_block_internal_decision(self):
        from iam.clock import life_moment
        from iam.domain import new_id
        self.c.start_session()
        self.c.schedule_moment(life_moment(new_id("mom"), self.c.life_time_ms, "int_Internal01"))
        provider = ScriptedProvider([Reply((rest(),))])
        bridge = CreatorBridge(self.rx, self.tx, USER)
        cycle = Cycle(self.c, provider, StubMemory(), StubWorld(), StubCapabilities(), bridge)
        self.api.mode = "drop"
        reports = Runner(cycle, self.c, bridge).run_pending()
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(self.rx.offset, 0)
        self.assertTrue(any(e.type == "system.error" and e.payload.get("fault") == "telegram_poll"
                            for e in self.c.store.events()))
        for mode in ("429", "403", "403"):
            self.api.mode = mode
            bridge.poll_replies()
        faults = [e.payload for e in self.c.store.events() if e.type == "system.error"]
        self.assertEqual([p["code"] for p in faults if p.get("fault") == "telegram_poll"], [0, 429, 403])
        self.assertFalse(any(p.get("fault") == "idempotency_conflict" for p in faults))
