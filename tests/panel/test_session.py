"""Поведение ручного сеанса, включая реальный цикл и постоянный учёт неудачных попыток."""
from unittest import mock

from iam.backup import NotEnoughSpace, SpaceGuard
from iam.cycle import Cycle, Runner
from iam.domain import new_id
from iam.interfaces import ProviderError
from iam.providers import ProviderAuthError, SessionLimitReached
from iam.session import SessionConflict, SessionRunner
from iam.state import EventSpec, verify
from iam.stubs import Reply, ScriptedProvider, StubCapabilities, StubCreatorBridge, StubMemory, StubWorld, rest
from state.helpers import StateCase


class SessionTest(StateCase):
    def build(self):
        self.c = self.controller()
        self.s = SessionRunner(self.c)
        return self.s

    def test_limits_pause_life_and_new_session(self):
        s = self.build()
        self.assertEqual(s.usage()["calls"], 0)
        s.command("start", "ручной запуск", max_calls=2, max_ms=5000)
        sid = self.c.session_id
        with self.assertRaises(SessionConflict):
            s.command("start", "второй сеанс")
        s.charge()
        self.time.advance(1000)
        s.command("pause", "проверка паузы")
        self.time.advance(10_000)
        self.assertEqual(s.usage()["elapsed_ms"], 1000)
        s.command("resume", "продолжить")
        s.charge()
        s.check_limits()
        self.assertEqual(self.c.session_state, "paused")
        self.assertEqual(self.c.state["world_time"]["pause_reason"], "session_limit")
        with self.assertRaises(SessionConflict):
            s.command("resume", "обойти лимит")
        s.command("stop", "конец")
        self.assertEqual(s.usage()["calls"], 2)
        s.command("start", "следующий")
        self.assertNotEqual(self.c.session_id, sid)
        self.assertEqual(s.usage()["calls"], 0)
        self.assertEqual(verify(self.c.store), [])
        operators = [e for e in self.c.events_after(0) if e.type == "operator.intervention"]
        self.assertTrue(operators)
        self.assertTrue(all(e.source.kind == "operator_panel" for e in operators))

    def test_time_limit_even_without_any_model_calls(self):
        s = self.build()
        s.command("start", "время", max_ms=1000)
        s.check_limits()
        self.time.advance(1000)
        s.check_limits()
        self.assertEqual(self.c.session_state, "paused")
        self.assertEqual(s.exhausted(), "time")
        s.check_limits()

    def test_charge_is_committed_without_invalidating_request_version(self):
        s = self.build()
        s.command("start", "учёт", max_calls=1)
        provider = ScriptedProvider([Reply(proposals=(rest(),))])
        bridge = StubCreatorBridge()
        cycle = Cycle(self.c, s.provider(provider), StubMemory(), StubWorld(), StubCapabilities(), bridge)
        runner = Runner(cycle, self.c, bridge)
        bridge.say("Не вызывать инструменты", "1")
        reports = s.step(runner)
        self.assertEqual(reports[0].outcome, "done")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(s.usage()["calls"], 1)
        self.assertEqual(self.c.session_state, "paused")
        self.assertEqual(verify(self.c.store), [])
        self.assertEqual(s.ledger.read(self.c.session_id)["attempts"][0]["request_id"], provider.calls[0]["request_id"])

    def test_errors_and_restart_do_not_reset_budget(self):
        s = self.build()
        s.command("start", "авария", max_calls=2)
        p = s.provider(ScriptedProvider([Reply(error=ProviderError("сбой"))]))
        with self.assertRaises(ProviderError):
            p.propose({"request_id": "req_Example0001"})
        self.assertEqual(s.usage()["calls"], 1)
        old_sid = self.c.session_id
        self.c = self.reopen(self.c)
        s = SessionRunner(self.c)
        self.assertEqual(self.c.session_id, old_sid)
        self.assertEqual(self.c.session_state, "paused")
        self.assertEqual(s.usage()["calls"], 1)
        s.command("resume", "после аварии")
        s.charge()
        with self.assertRaises(SessionLimitReached):
            s.charge()
        self.assertEqual(s.usage()["calls"], 2)
        self.assertEqual(verify(self.c.store), [])

    def test_auth_failure_pauses_and_uncounted_provider_uses_cli_meter(self):
        s = self.build()
        s.command("start", "авторизация")
        p = s.provider(ScriptedProvider([Reply(error=ProviderAuthError("вход"))]), charge=False)
        with self.assertRaises(ProviderAuthError):
            p.propose({"request_id": "req_Example0001"})
        self.assertEqual(self.c.session_state, "paused")
        self.assertEqual(s.usage()["calls"], 0)
        with self.assertRaises(ProviderError):
            s.charge()

    def test_time_is_checked_while_model_is_in_flight(self):
        s = self.build()
        s.command("start", "в полёте", max_calls=1, max_ms=10)
        s.charge()
        s.in_flight = True
        s.check_limits()
        self.assertEqual(self.c.session_state, "running")
        self.time.advance(10)
        s.check_limits()
        self.assertEqual(self.c.session_state, "paused")
        with self.assertRaises(SessionConflict):
            s.command("resume", "пока старый ответ не пришёл")
        s.command("stop", "остановка")
        with self.assertRaises(SessionConflict):
            s.command("start", "пока старый ответ не пришёл")

    def test_invalid_controls_and_ledger_race_are_refused(self):
        s = self.build()
        for kw in ({"max_calls":0}, {"max_calls":True}, {"max_ms":0}, {"max_ms":False}, {"max_calls":11}, {"max_ms":1_800_001}):
            with self.assertRaises(ValueError):
                s.command("start", "лимит", **kw)
        with self.assertRaises(ValueError):
            s.command("start", " ")
        with self.assertRaises(ValueError):
            s.audit("start", " ")
        with self.assertRaises(SessionConflict):
            s.command("invalid", "неизвестная")
        s.command("start", "запуск")
        with mock.patch.object(s.ledger, "reserve", return_value=False):
            with self.assertRaises(SessionLimitReached):
                s.charge()
        with mock.patch.object(self.c, "intervene", return_value=mock.Mock(applied=False, reason="denied")):
            with self.assertRaisesRegex(SessionConflict, "denied"):
                s.audit("pause", "отказ ядра")

    def test_old_session_without_panel_config_and_disk_full(self):
        s = self.build()
        self.c.start_session()
        self.time.advance(20)
        self.assertEqual(s.usage()["elapsed_ms"], 20)
        guard = SpaceGuard(self.db, reserve=100, usage=lambda p: (100,100,0))
        s.space = guard
        with self.assertRaises(NotEnoughSpace):
            s.step(mock.Mock())
        self.assertEqual(self.c.session_state, "stopped")
        self.assertEqual(verify(self.c.store), [])

    def test_durable_reserve_rejects_eleventh_attempt(self):
        s = self.build()
        s.command("start", "учёт", max_calls=1)
        self.assertTrue(s.ledger.reserve(self.c.session_id, 1, new_id("req")))
        self.assertFalse(s.ledger.reserve(self.c.session_id, 1, new_id("req")))

    def test_model_cannot_change_or_remove_panel_budget_through_parent_path(self):
        from iam.domain import Op
        s = self.build()
        s.command("start", "защита")
        before = s.config()
        for path, value in (("world.panel_session.max_calls",1000),("world",{})):
            result = self.c.apply(self.prop(self.c,Op("world_time",path,"set",value,True),actor="model"))
            self.assertEqual(result.status,"rejected")
        self.assertEqual(s.config(),before)

    def test_s08_session_migrates_known_and_unanswered_usage_once(self):
        c=self.controller(running=True)
        provider=ScriptedProvider([Reply(proposals=(rest(),),extra={"usage":{"calls":2,"duration_ms":1}}),
                                   Reply(error=ProviderError("обрыв"))])
        bridge=StubCreatorBridge()
        cycle=Cycle(c,provider,StubMemory(),StubWorld(),StubCapabilities(),bridge)
        runner=Runner(cycle,c,bridge,max_rounds=1)
        bridge.say("первое","1");runner.run_pending()
        bridge.say("второе","2");runner.run_pending()
        s=SessionRunner(c)
        self.assertEqual(s.usage()["calls"],4)
        again=SessionRunner(c)
        self.assertEqual(again.usage()["calls"],4)
        again.command("pause","пауза")
        again.command("resume","продолжить")
        self.assertEqual(again.usage()["calls"],4)
