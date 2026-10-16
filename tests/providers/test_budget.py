"""Бюджет сеанса и техническая пауза (R-191; сценарий «Ошибка авторизации или лимит Plus»; PLAN §5)."""
import unittest

from iam.cycle import Cycle, Runner
from iam.providers import (BudgetedRunner, ClaudeCodeProvider, FakeClaude, PausingProvider, ProviderAuthError,
                           ProviderUnavailable, SandboxConfig, SessionBudget, SessionLimitReached)
from iam.providers import fake
from iam.interfaces import ProviderError
from iam.stubs import StubCapabilities, StubCreatorBridge, StubMemory, StubWorld

from cycle.helpers import INTENTION, CycleCase

NOTE = {"kind": "no_action", "rationale": "жду", "cites": [], "body": {"reason": "тихо"}}


class BudgetCounters(unittest.TestCase):
    def test_defaults_are_30_minutes_and_10_calls(self):
        b = SessionBudget()
        self.assertEqual((b.max_calls, b.max_ms), (10, 30 * 60_000))

    def test_calls_threshold_alone(self):
        b = SessionBudget()
        self.assertEqual([b.charge("ses_A", t) for t in range(10)], [None] * 10)
        self.assertEqual(b.charge("ses_A", 11), "calls")
        self.assertEqual(b.calls, 10)                     # отказ обращение не списывает

    def test_time_threshold_alone(self):
        b = SessionBudget()
        self.assertIsNone(b.charge("ses_A", 1_000))
        self.assertIsNone(b.charge("ses_A", 1_000 + 30 * 60_000 - 1))
        self.assertEqual(b.charge("ses_A", 1_000 + 30 * 60_000), "time")

    def test_whichever_comes_first(self):
        by_calls = SessionBudget(max_calls=3)
        [by_calls.charge("s", i) for i in range(3)]
        self.assertEqual(by_calls.charge("s", 100), "calls")          # 3 обращения за 100 мс — раньше времени
        by_time = SessionBudget(max_calls=3, max_ms=1000)
        by_time.charge("s", 0)
        self.assertEqual(by_time.charge("s", 1000), "time")           # 1 обращение, но время вышло
        both = SessionBudget(max_calls=1, max_ms=1000)
        both.charge("s", 0)
        self.assertEqual(both.charge("s", 5000), "time")              # вышло всё сразу: называем время

    def test_new_session_starts_a_new_budget(self):
        b = SessionBudget(max_calls=1)
        b.charge("ses_A", 0)
        self.assertEqual(b.charge("ses_A", 1), "calls")
        self.assertIsNone(b.charge("ses_B", 2))


class PauseCase(CycleCase):
    """Личность с желанием в состоянии; провайдер — ClaudeCodeProvider на FakeClaude под бюджетом и паузой."""

    def wire(self, *results, budget=None, threshold=2):
        self.build()
        self.fk = FakeClaude(results)
        self.budget = budget or SessionBudget()
        inner = ClaudeCodeProvider(BudgetedRunner(self.fk, self.budget, self.c), SandboxConfig())
        self.provider = PausingProvider(inner, self.c, failure_threshold=threshold)
        self.cycle = Cycle(self.c, self.provider, StubMemory(), StubWorld(), StubCapabilities(), StubCreatorBridge())
        self.runner = Runner(self.cycle, self.c, StubCreatorBridge())

    def pauses(self):
        return [e for e in self.c.store.events() if e.type == "session.paused"]

    def events(self, kind):
        return [e for e in self.c.store.events() if e.type == kind]

    def assertQuiet(self, requests_before):
        """После паузы модель не вызывается и состояние не меняется."""
        state = self.c.state
        self.assertEqual(self.runner.run_pending(), [])
        self.assertEqual(len(self.events("proposal.requested")), requests_before)
        self.assertEqual(self.c.state, state)


class AuthAndLimitScenarioTest(PauseCase):
    """scenarios/required/auth_error_or_limit.toml."""

    def test_auth_error_pauses_with_auth_lost_and_saves_state(self):
        self.wire(fake.auth_error(), fake.reply([NOTE]))
        first, second = self.due(), self.due()
        before = self.c.state["intentions"][INTENTION]
        report = self.cycle.run(first)
        self.assertEqual((report.outcome, report.reason), ("no_decision", "provider_error"))
        (paused,) = self.pauses()
        self.assertEqual(paused.payload["reason"], "auth_lost")
        self.assertEqual(self.c.session_state, "paused")
        errors = [e for e in self.events("system.error") if e.payload.get("fault") == "auth"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.events("system.limit_reached"), [])
        self.assertEqual(self.c.state["intentions"][INTENTION], before)
        self.assertEqual(len(self.fk.calls), 1)
        self.assertQuiet(len(self.events("proposal.requested")))
        self.assertEqual(len(self.fk.calls), 1)          # второй повод ждёт возобновления

    def test_subscription_limit_is_recorded_and_pauses_as_auth_lost(self):
        self.wire(fake.rate_limited(), fake.reply([NOTE]))
        report = self.cycle.run(self.due())
        self.assertEqual(report.outcome, "no_decision")
        (limit,) = self.events("system.limit_reached")
        self.assertEqual((limit.payload["kind"], limit.payload["reason"]), ("subscription", "auth_lost"))
        self.assertEqual(limit.idempotency_key, f"lim:{self.c.session_id}:subscription")
        (paused,) = self.pauses()
        self.assertEqual(paused.payload["reason"], "auth_lost")
        self.assertLess(limit.seq, paused.seq)                         # сначала запись о лимите, потом пауза
        self.assertEqual(self.c.state["world_time"]["world"]["limits"]["subscription"]["reason"], "auth_lost")
        self.assertEqual(self.c.state["intentions"][INTENTION]["status"], "active")
        self.assertQuiet(1)

    def test_runs_are_told_apart_by_the_journal(self):
        self.wire(fake.auth_error())
        self.cycle.run(self.due())
        self.assertEqual(self.events("system.limit_reached"), [])      # у ошибки входа записи о лимите нет

    def test_resume_allows_work_again(self):
        self.wire(fake.auth_error(), fake.reply([NOTE]))
        self.cycle.run(self.due())
        self.assertTrue(self.c.resume_session().applied)
        report = self.cycle.run(self.due())
        self.assertEqual(report.outcome, "done")


class SessionBudgetPauseTest(PauseCase):
    def test_eleventh_call_is_refused_and_pauses_as_session_limit(self):
        self.wire(*[fake.reply([NOTE]) for _ in range(11)])
        done = [self.cycle.run(self.due()).outcome for _ in range(10)]
        self.assertEqual(done, ["done"] * 10)
        report = self.cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("no_decision", "provider_error"))
        self.assertEqual(len(self.fk.calls), 10)                        # одиннадцатый запуск не состоялся
        (limit,) = self.events("system.limit_reached")
        self.assertEqual(limit.payload["kind"], "calls")
        self.assertEqual(self.pauses()[0].payload["reason"], "session_limit")

    def test_thirty_minutes_stop_the_session_before_the_call_limit(self):
        self.wire(*[fake.reply([NOTE]) for _ in range(3)])
        self.assertEqual(self.cycle.run(self.due()).outcome, "done")
        self.time.advance(30 * 60_000)
        report = self.cycle.run(self.due())
        self.assertEqual(report.outcome, "no_decision")
        (limit,) = self.events("system.limit_reached")
        self.assertEqual(limit.payload["kind"], "time")
        self.assertEqual(self.pauses()[0].payload["reason"], "session_limit")
        self.assertEqual(len(self.fk.calls), 1)

    def test_format_retry_costs_a_call(self):
        self.wire(fake.garbage(), fake.reply([NOTE]), budget=SessionBudget(max_calls=2))
        self.assertEqual(self.cycle.run(self.due()).outcome, "done")
        self.assertEqual(self.budget.calls, 2)
        self.assertEqual(self.cycle.run(self.due()).outcome, "no_decision")
        self.assertEqual(len(self.fk.calls), 2)

    def test_budget_applies_per_session(self):
        self.wire(*[fake.reply([NOTE]) for _ in range(4)], budget=SessionBudget(max_calls=2))
        [self.cycle.run(self.due()) for _ in range(2)]
        self.assertTrue(self.c.stop_session().applied)
        self.assertTrue(self.c.start_session().applied)
        self.assertEqual(self.cycle.run(self.due()).outcome, "done")     # новый сеанс — новый бюджет
        self.assertEqual(self.budget.calls, 1)


class RepeatedFailureTest(PauseCase):
    def test_second_failure_in_a_row_pauses(self):
        self.wire(fake.overloaded(), fake.overloaded(), fake.reply([NOTE]))
        self.cycle.run(self.due())
        self.assertEqual(self.c.session_state, "running")              # одиночный сбой — не повод для паузы
        self.cycle.run(self.due())
        self.assertEqual(self.c.session_state, "paused")
        self.assertEqual(self.pauses()[0].payload["reason"], "repeated_failure")
        self.assertQuiet(2)

    def test_success_resets_the_failure_count(self):
        self.wire(fake.overloaded(), fake.reply([NOTE]), fake.overloaded(), fake.reply([NOTE]))
        for _ in range(4):
            self.cycle.run(self.due())
        self.assertEqual(self.c.session_state, "running")
        self.assertEqual(self.pauses(), [])

    def test_threshold_is_configurable(self):
        self.wire(fake.overloaded(), threshold=1)
        self.cycle.run(self.due())
        self.assertEqual(self.pauses()[0].payload["reason"], "repeated_failure")

    def test_provider_is_not_called_outside_a_running_session(self):
        self.wire(fake.reply([NOTE]))
        self.c.pause_session()
        with self.assertRaises(ProviderError):
            self.provider.propose({})
        self.assertEqual(self.fk.calls, [])
        self.assertEqual(self.provider.name, "claude-code")

    def test_errors_pass_through_unchanged(self):
        self.wire(fake.auth_error(), fake.overloaded(), fake.reply([NOTE]))
        req = lambda: {"request_id": "req_Direct00001", "epoch": 1, "state_version": 1, "purpose": "choose_action",
                       "context": [], "allowed_proposals": ["no_action"], "allowed_actions": [],
                       "limits": {"max_output_tokens": 1, "deadline_ms": 1, "attempt": 0}}
        with self.assertRaises(ProviderAuthError):
            self.provider.propose(req())
        self.c.resume_session()
        with self.assertRaises(ProviderUnavailable):
            self.provider.propose(req())
        self.assertEqual(self.provider.propose(req())["status"], "ok")

    def test_budgeted_runner_raises_a_typed_error(self):
        b = SessionBudget(max_calls=0)
        runner = BudgetedRunner(FakeClaude(), b, self.controller(running=True))
        with self.assertRaises(SessionLimitReached) as ctx:
            runner(["x"], "", 1.0)
        self.assertEqual(ctx.exception.kind, "calls")


if __name__ == "__main__":
    unittest.main()
