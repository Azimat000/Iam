"""Настоящий запуск процесса (stdin → stdout), таймаут с очисткой, отсутствие программы; сквозные проходы цикла."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from iam.cycle import Cycle
from iam.providers import ClaudeCodeProvider, FakeClaude, ProviderTimeout, SandboxConfig, classify, subprocess_runner
from iam.providers import fake
from iam.domain import Op
from iam.stubs import Reply, ScriptedProvider, act, rest

from cycle.helpers import CAP_NOTES, INTENTION, CycleCase

CLI = Path(__file__).resolve().parent / "fake_cli.py"


class SubprocessRunnerTest(unittest.TestCase):
    def test_context_in_stdin_and_result_from_stdout_only(self):
        with tempfile.TemporaryDirectory() as d:
            before = set(Path(d).iterdir())
            res = subprocess_runner([sys.executable, str(CLI), "echo"], "привет, мир", 20.0)
            self.assertEqual((res.returncode, res.stdout.strip(), res.timed_out), (0, "получено:привет, мир", False))
            self.assertEqual(set(Path(d).iterdir()), before)    # файлов обмена нет

    def test_nonzero_exit_keeps_stderr(self):
        res = subprocess_runner([sys.executable, str(CLI), "fail"], "", 20.0)
        self.assertEqual((res.returncode, res.stderr.strip()), (3, "сломалось"))
        self.assertEqual(classify(res).kind.value, "exec")

    def test_timeout_kills_and_runs_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "cleaned"
            cleanup = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('1')"]
            res = subprocess_runner([sys.executable, str(CLI), "hang"], "", 0.5, cleanup)
            self.assertEqual((res.returncode, res.timed_out), (None, True))
            self.assertTrue(marker.exists())
            self.assertEqual(classify(res).kind.value, "timeout")
            plain = subprocess_runner([sys.executable, str(CLI), "hang"], "", 0.5)
            self.assertTrue(plain.timed_out)

    def test_missing_program_is_code_127_not_an_exception(self):
        res = subprocess_runner(["нет-такой-программы-iam"], "", 5.0)
        self.assertEqual(res.returncode, 127)
        self.assertIn("не найдена программа", res.stderr)
        self.assertEqual(classify(res).kind.value, "exec")

    def test_provider_with_the_real_runner_reports_a_timeout(self):
        class Slow:
            """Исполнитель, который зависает: подставляем настоящий запуск вместо docker."""
            def __call__(self, argv, stdin, timeout_s, cleanup=None):
                return subprocess_runner([sys.executable, str(CLI), "hang"], stdin, timeout_s, None)
        p = ClaudeCodeProvider(Slow(), SandboxConfig(), timeout_s=0.5)
        with self.assertRaises(ProviderTimeout):
            p.propose({"request_id": "req_Slow000001", "epoch": 1, "state_version": 1, "purpose": "choose_action",
                       "context": [], "allowed_proposals": ["no_action"], "allowed_actions": [],
                       "limits": {"max_output_tokens": 1, "deadline_ms": 1, "attempt": 0}})


NOTE = {"kind": "no_action", "rationale": "жду", "cites": [], "body": {"reason": "тихо"}}


class CycleOnClaudeCodeTest(CycleCase):
    """Цикл S07 на провайдере S08 (FakeClaude вместо docker)."""

    def wire(self, *results, **kw):
        self.build(**kw)
        self.fk = FakeClaude(results)
        self.provider = ClaudeCodeProvider(self.fk, SandboxConfig())
        self.cycle = Cycle(self.c, self.provider, self.memory, self.world, self.caps, self.creator)

    def test_full_pass_with_an_action(self):
        proposal = {"kind": "action", "rationale": "надо записать", "cites": [], "body": {"capability_id": CAP_NOTES, "params": {"t": 1}}}
        self.wire(fake.reply([proposal]))
        report = self.cycle.run(self.due())
        self.assertEqual((report.outcome, report.actions[0].status), ("done", "confirmed"))
        self.assertEqual(self.world.executed[0].capability_id, CAP_NOTES)
        sent = self.fk.calls[0]["stdin"]
        self.assertIn(CAP_NOTES, sent)
        self.assertNotIn(self.c.persona_id, sent)

    def test_tool_attempt_result_is_not_applied(self):
        """R-117: попытка инструмента делает результат непригодным — ничего не применено и не выполнено."""
        proposal = {"kind": "action", "rationale": "сделаю", "cites": [], "body": {"capability_id": CAP_NOTES, "params": {}}}
        self.wire(fake.tool_attempt([proposal]))
        before = self.c.state
        report = self.cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("rejected", "tool_use_detected"))
        self.assertEqual(self.world.executed, [])
        self.assertNotIn("action.attempted", self.types())
        self.assertIn("system.error", self.types())
        self.assertEqual(self.c.state["intentions"], before["intentions"])

    def test_garbage_then_valid_answer_in_the_same_request(self):
        self.wire(fake.garbage(), fake.reply([NOTE]))
        report = self.cycle.run(self.due())
        self.assertEqual((report.outcome, len(report.requests), len(self.fk.calls)), ("done", 1, 2))

    def test_validator_works_without_isolation_and_does_not_assume_a_tool_less_model(self):
        """R-118: изоляция и проверка результата — два независимых слоя. Валидатор отклоняет «выполнено» в теле предложения,
        даже если исполнитель ничего про инструменты не сообщил (допускаем, что модель могла их использовать)."""
        sneaky = {"kind": "action", "rationale": "r", "cites": [],
                  "body": {"capability_id": CAP_NOTES, "params": {}, "stdout": "файл создан", "executed": True}}
        self.wire(fake.reply([sneaky]))           # permission_denials пусты, tool_use_detected исполнителя = False
        report = self.cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("rejected", "tool_use_detected"))
        self.assertEqual(self.world.executed, [])


class ModelSwapTest(CycleCase):
    """Сценарий «Смена модели» (scenarios/required/model_swap.toml): состояние и память переживают замену, старый
    ответ после замены устаревает. Вмешательство `edit_config` (запись самой замены) появится в S10; здесь версию
    меняет `technical_fix` — оно так же подменяет состояние под ответом, который ещё в пути."""

    def test_swap_keeps_state_and_memory(self):
        self.build()
        provider_a = ScriptedProvider([Reply((rest("думаю"),))])
        cycle_a = Cycle(self.c, provider_a, self.memory, self.world, self.caps, self.creator)
        self.assertEqual(cycle_a.run(self.due()).outcome, "done")
        version_before, state_before = self.c.state_version, self.c.state
        fk = FakeClaude([fake.reply([NOTE])])
        cycle_b = Cycle(self.c, ClaudeCodeProvider(fk, SandboxConfig()), self.memory, self.world, self.caps, self.creator)
        self.assertEqual((self.c.state_version, self.c.state), (version_before, state_before))   # замена версию не меняет
        report = cycle_b.run(self.due())
        self.assertEqual(report.outcome, "done")
        self.assertEqual(self.c.state["intentions"][INTENTION], state_before["intentions"][INTENTION])
        self.assertEqual(json.loads(fk.calls[0]["stdin"])["purpose"], "choose_action")

    def test_answer_of_the_old_provider_after_the_swap_is_stale(self):
        self.build()
        old = ScriptedProvider()
        cycle = Cycle(self.c, old, self.memory, self.world, self.caps, self.creator, max_rerequest=0)
        applied = []

        def swap_during_thinking(request):
            applied.append(self.c.intervene("technical_fix", "замена поставщика интеллекта A → B",
                                            [Op("preferences", "provider", "set", "B", True)], target="config").applied)
        old.queue(Reply((act(CAP_NOTES),), before=swap_during_thinking))
        report = cycle.run(self.due())
        self.assertEqual(applied, [True])
        self.assertEqual((report.outcome, report.reason), ("no_decision", "version"))
        self.assertIn("proposal.stale", self.types())
        self.assertNotIn("proposal.accepted", self.types())
        self.assertEqual(self.world.executed, [])
        self.assertEqual(self.c.state["preferences"]["provider"], "B")


if __name__ == "__main__":
    unittest.main()
