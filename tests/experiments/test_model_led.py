"""A на реальном ядре и общем стенде. Ответы модели — явно заданные заглушки."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from iam.domain import Op
from iam.experiments import Bench, Persona, Scenario
from iam.experiments.runtime import CAP_NOTES, Runtime
from iam.experiments.variants.model_led import ModelLedArchitecture
from iam.experiments.variants.model_led.context import MAX_TEXT, bounded, numeric_fields, state_fragment
from iam.experiments.variants.model_led.provider import GroundedProvider
from iam.providers.claude_code import build_prompt
from iam.providers import ClaudeCodeProvider, FakeClaude, SandboxConfig, fake
from iam.providers.errors import ProviderFormatError
from iam.state import verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, attend, change, rest

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "scenarios/experiments"
PROFILE = ROOT / "scenarios/personas/curious.toml"


def ticket():
    return {"format": 1, "id": "model_led_case", "group": "experiments", "persona": "any",
            "step": [{"id": "start", "kind": "session", "action": "start"},
                     {"id": "choose", "kind": "decide", "situation": "Выбрать следующее занятие"}]}


def refs(request):
    return list(dict.fromkeys(c["ref"] for c in request["context"] if c["ref"].startswith(("evt_", "mem_"))))


def grounded(request):
    return Reply(proposals=(rest("Обдумать доступный опыт", cites=refs(request)),))


def scripted_factory(runtime):
    """Технические ответы, а не имитация понимания сценария/качества личности."""
    def answer(request):
        if runtime.model_steps:
            fixture = runtime.model_steps.pop(0)
            if fixture["proposal"] == "action":
                proposal = act("cap_MissingPlotter01", executed=True, cites=refs(request))
            elif fixture["proposal"] == "attend":
                proposal = attend("activity", cites=refs(request))
            else:
                proposal = change([{"group": "intentions", "path": "botany", "op": "set",
                                    "value": {"text": fixture["content"], "status": "active"}}], cites=refs(request))
        else:
            proposal = rest("Тестовое обдумывание", cites=refs(request))
        return Reply(proposals=(proposal,))

    class Provider(ScriptedProvider):
        def propose(self, request):
            self.queue(answer)
            return super().propose(request)
    return Provider()


class ModelLedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iam-s12-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bench = Bench(self.root)
        self.persona = Persona.load(PROFILE)

    def run_replies(self, *replies, data=None):
        self.provider = ScriptedProvider(replies)
        self.result = self.bench.run(Scenario.from_dict(data or ticket()),
                                     ModelLedArchitecture(lambda runtime: self.provider), self.persona)[0]
        self.decision = self.result.trace["decision_trace"][0]
        return self.decision["report"]

    def runtime(self):
        store = Store.create(self.root / "runtime.db", "exp_ModelLedTest01", created_at="2026-01-01T00:00:00.000Z")
        self.addCleanup(store.close)
        runtime = Runtime(store, self.persona, 42, {"memory_off": False, "motivation_off": False})
        runtime.controller.start_session()
        return runtime

    def test_eight_scenarios_twice_with_saved_repeatable_traces_and_real_core(self):
        self.assertEqual(len(list(CATALOG.glob("*.toml"))), 8)
        for scenario in sorted(CATALOG.glob("*.toml")):
            with self.subTest(scenario=scenario.name):
                first = self.bench.run(scenario, ModelLedArchitecture(scripted_factory), self.persona, seed=42)
                second = self.bench.run(scenario, ModelLedArchitecture(scripted_factory), self.persona, seed=42)
                for a, b in zip(first, second, strict=True):
                    self.assertEqual(a.trace_bytes, b.trace_bytes)
                    self.assertEqual(a.trace["architecture"], "A-model-led")
                    self.assertEqual(a.trace["assertions_status"], "not_evaluated")
                    self.assertEqual(a.trace["status"], "completed")
                    self.assertTrue((a.path / "manifest.json").is_file())
                    self.assertEqual((a.path / "trace.json").read_bytes(), a.trace_bytes)
                    with Store.open(a.path / "persona.db", readonly=True) as store:
                        self.assertEqual(verify(store), [])
                        self.assertEqual(store.integrity_problems(), [])
                    for decision in a.trace["decision_trace"]:
                        self.assertIn(decision["report"]["outcome"], ("done", "rejected"))
                        context_refs = {c["ref"] for req in decision["requests"] for c in req["context"]}
                        for received in decision["proposals"]:
                            for proposal in received["response"]["proposals"]:
                                self.assertTrue(proposal["cites"])
                                self.assertLessEqual(set(proposal["cites"]), context_refs)
                    if scenario.stem == "exp_no_tool":
                        self.assertTrue(any(e["type"] == "proposal.rejected" for e in a.trace["journal"]))
                        self.assertFalse(any(e["type"] == "action.confirmed" for e in a.trace["journal"]))

    def test_memory_citations_and_disabled_memory(self):
        warm, cold, off = self.bench.run(CATALOG / "exp_diff_experience.toml",
                                        ModelLedArchitecture(scripted_factory), self.persona)
        for result in (warm, cold):
            d = result.trace["decision_trace"][0]
            memory = {m["memory_id"] for m in d["memory"]}
            cites = set(d["proposals"][0]["response"]["proposals"][0]["cites"])
            self.assertTrue(memory)
            self.assertLessEqual(memory, cites)
        d = off.trace["decision_trace"][0]
        self.assertEqual(d["memory"], [])
        self.assertFalse(any(c.startswith("mem_") for c in d["proposals"][0]["response"]["proposals"][0]["cites"]))

    def test_unread_never_reaches_request_or_outbound_prompt(self):
        result = self.bench.run(CATALOG / "exp_unread.toml", ModelLedArchitecture(scripted_factory), self.persona)[0]
        self.assertIn("КРАСНЫЙ-ЯКОРЬ-91", result.trace_bytes.decode())
        for decision in result.trace["decision_trace"]:
            self.assertNotIn("КРАСНЫЙ-ЯКОРЬ-91", json.dumps(decision, ensure_ascii=False))
            for request in decision["requests"]:
                self.assertNotIn("КРАСНЫЙ-ЯКОРЬ-91", build_prompt(request))
        self.assertFalse(any(e["type"] == "input.read" for e in result.trace["journal"]))

    def test_only_selected_state_fields_and_no_technical_envelope_in_outbound_prompt(self):
        runtime = self.runtime()
        for group, path, value in (("affect", "read_inputs.private", "PRIVATE-READ"),
                                   ("world_time", "world.private", "PRIVATE-ARCHIVE"),
                                   ("selfmodel", "private", "PRIVATE-SELF"),
                                   ("traits", "private", "PRIVATE-TRAIT"),
                                   ("affect", "energy", 0.7),
                                   ("intentions", "note", {"text": "Закончить заметку", "status": "active",
                                                           "private": "PRIVATE-INTENTION"})):
            runtime.move([Op(group, path, "set", value, True)], "тестовая предыстория")
        provider = ScriptedProvider([grounded])
        cycle = ModelLedArchitecture(lambda _: provider).build(runtime)
        report = cycle.run(runtime.trigger("choose"))
        self.assertEqual(report.outcome, "done")
        request = provider.calls[0]
        prompt = build_prompt(request)
        self.assertNotIn("PRIVATE-", prompt)
        self.assertIn("Закончить заметку", prompt)
        self.assertIn('"energy": 0.7', json.loads(prompt)["context"][-1]["text"])
        for key in ("persona_id", "session_id", "idempotency_key", "issued_at", "state_version"):
            self.assertNotIn(key, json.loads(prompt))

    def test_context_limits_and_non_numeric_fields(self):
        self.assertEqual(bounded("short"), "short")
        self.assertEqual(bounded("x" * (MAX_TEXT + 5)), "x" * MAX_TEXT + " [фрагмент обрезан]")
        self.assertEqual(numeric_fields({"bad": True, "secret": {"x": "PRIVATE"}, "nan": float("nan"),
                                        "inf": float("inf"), "a" * 65: 1, "ok": 0.5}), {"ok": 0.5})
        state = {"traits": {}, "intentions": {"bad": "text", "okay": {"text": "x" * 1300}},
                 "affect": {"arousal": False, "valence": float("inf")}}
        selected = state_fragment(state)
        self.assertEqual(selected["affect"], {})
        self.assertEqual(len(selected["intentions"]), 1)
        self.assertIn("обрезан", selected["intentions"][0]["text"])

    def test_motivation_off_hides_actual_motives(self):
        result = self.bench.run(Scenario.from_dict(ticket()), ModelLedArchitecture(scripted_factory),
                                self.persona, motivation_off=True)[0]
        text = json.dumps(result.trace["decision_trace"][0]["requests"], ensure_ascii=False)
        for motive in self.persona.motives:
            self.assertNotIn(motive, text)

    def test_bad_format_repaired_once_with_link_to_first_request(self):
        report = self.run_replies(Reply(raw={"bad": True}), grounded)
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(len(self.provider.calls), 2)
        first, second = self.provider.calls
        self.assertEqual(second["limits"]["attempt"], 1)
        self.assertEqual(second["retry_of"], first["request_id"])
        self.assertIn("единственная попытка исправления", build_prompt(second))
        self.assertNotIn("единственная попытка исправления", build_prompt(first))

    def test_claude_adapter_with_fake_process_parses_json_and_repairs_only_once(self):
        calls = []
        def runner(argv, stdin, timeout_s, cleanup):
            calls.append(stdin)
            if len(calls) == 1:
                return fake.garbage()
            view = json.loads(stdin)
            proposal = {"kind": "no_action", "body": {"reason": "отдых"}, "rationale": "по ситуации",
                        "cites": [view["context"][0]["ref"]]}
            return fake.reply([proposal])
        adapter = ClaudeCodeProvider(runner, SandboxConfig(), max_format_retries=0)
        result = self.bench.run(Scenario.from_dict(ticket()), ModelLedArchitecture(lambda _: adapter), self.persona)[0]
        self.assertEqual(result.trace["decision_trace"][0]["report"]["outcome"], "done")
        self.assertEqual(len(calls), 2)
        self.assertIn("единственная попытка исправления", calls[1])

    def test_fake_claude_tool_attempt_is_rejected_without_retry(self):
        runner = FakeClaude([fake.tool_attempt()])
        adapter = ClaudeCodeProvider(runner, SandboxConfig(), max_format_retries=0)
        result = self.bench.run(Scenario.from_dict(ticket()), ModelLedArchitecture(lambda _: adapter), self.persona)[0]
        self.assertEqual(result.trace["decision_trace"][0]["report"]["outcome"], "rejected")
        self.assertEqual(len(runner.calls), 1)

    def test_fake_claude_second_bad_format_does_not_launch_third_process(self):
        runner = FakeClaude([fake.garbage(), fake.garbage(), fake.garbage()])
        adapter = ClaudeCodeProvider(runner, SandboxConfig(), max_format_retries=0)
        result = self.bench.run(Scenario.from_dict(ticket()), ModelLedArchitecture(lambda _: adapter), self.persona)[0]
        self.assertEqual(result.trace["decision_trace"][0]["report"]["outcome"], "rejected")
        self.assertEqual(len(runner.calls), 2)

    def test_second_bad_format_stops_without_action(self):
        report = self.run_replies(Reply(raw={}), Reply(raw=[]), grounded)
        self.assertEqual(report["outcome"], "rejected")
        self.assertEqual(report["actions"], [])
        self.assertEqual(len(self.provider.calls), 2)

    def test_provider_format_exception_uses_same_single_retry(self):
        report = self.run_replies(Reply(error=ProviderFormatError("RAW SECRET")), grounded)
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertNotIn("RAW SECRET", self.result.trace_bytes.decode())

    def test_missing_or_out_of_context_cites_not_applied_or_fabricated(self):
        for cites in ([], ["mem_NotAvailable01"], ["evt_NotAvailable01"]):
            with self.subTest(cites=cites):
                report = self.run_replies(Reply(proposals=(rest(cites=cites),)), grounded)
                self.assertEqual(report["outcome"], "done")
                self.assertEqual(len(self.provider.calls), 2)
                self.assertEqual(len(self.decision["proposals"]), 1)

    def test_existing_but_unretrieved_memory_cannot_be_cited(self):
        runtime = self.runtime()
        runtime.background([{"id": "hidden", "kind": "memory", "text": "Скрытый опыт"}])
        record = runtime.memory.retrieve("")[0]
        runtime.memory.off = True
        provider = ScriptedProvider([Reply(proposals=(rest(cites=[record.memory_id]),)), grounded])
        report = ModelLedArchitecture(lambda _: provider).build(runtime).run(runtime.trigger("test"))
        self.assertEqual(report.outcome, "done")
        self.assertEqual(len(provider.calls), 2)

    def test_refusal_and_error_do_not_trigger_repair_or_actions(self):
        for status in ("refused", "error"):
            with self.subTest(status=status):
                report = self.run_replies(Reply(status=status), grounded)
                self.assertEqual(report["outcome"], "refused")
                self.assertEqual(report["actions"], [])
                self.assertEqual(len(self.provider.calls), 1)

    def test_provider_exception_redacted_and_no_retry(self):
        report = self.run_replies(Reply(error=RuntimeError("RAW PRIVATE STDERR")), grounded)
        self.assertEqual(report["outcome"], "no_decision")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertNotIn("RAW PRIVATE STDERR", self.result.trace_bytes.decode())

    def test_tool_claim_rejected_by_core_without_format_repair(self):
        for reply in (Reply(proposals=(act(CAP_NOTES, executed=True),)),
                      Reply(proposals=(act(CAP_NOTES, {"tool_calls": ["shell"]}),)),
                      Reply(proposals=(rest(),), tool_use=True)):
            with self.subTest(reply=reply):
                report = self.run_replies(reply, grounded)
                self.assertEqual(report["outcome"], "rejected")
                self.assertEqual(report["actions"], [])
                self.assertEqual(len(self.provider.calls), 1)
                self.assertTrue(any(e["type"] == "proposal.rejected" for e in self.result.trace["journal"]))

    def test_unavailable_capability_rejected_by_core(self):
        def answer(request):
            return Reply(proposals=(act("cap_MissingTool01", cites=refs(request)),))
        report = self.run_replies(answer)
        self.assertEqual(report["outcome"], "rejected")
        self.assertEqual(report["actions"], [])

    def test_state_change_affect_is_checked_and_applied_by_core(self):
        def answer(request):
            return Reply(proposals=(change([{"group": "affect", "path": "valence", "op": "set", "value": 0.2}],
                                           cites=refs(request)),))
        report = self.run_replies(answer)
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.result.trace["final_state"]["affect"]["valence"], 0.2)

    def test_permissions_and_fabricated_completion_protected(self):
        for group, path, value in (("world_time", "capabilities.new", {"available": True}),
                                   ("world_time", "world.actions.act_FakeAction01.status", "confirmed")):
            def answer(request):
                return Reply(proposals=(change([{"group": group, "path": path, "op": "set", "value": value}],
                                               cites=refs(request)),))
            with self.subTest(path=path):
                report = self.run_replies(answer)
                self.assertEqual(report["outcome"], "rejected")
                self.assertEqual(report["actions"], [])

    def test_confirmed_action_is_from_world_and_causes_memory(self):
        def answer(request):
            return Reply(proposals=(act(CAP_NOTES, {"note": "заметка"}, cites=refs(request)),))
        report = self.run_replies(answer)
        self.assertEqual(report["actions"][0]["status"], "confirmed")
        self.assertIsNotNone(report["actions"][0]["memory_id"])
        events = self.result.trace["journal"]
        confirmed = next(e for e in events if e["type"] == "action.confirmed")
        self.assertEqual(confirmed["source"]["kind"], "world")

    def test_stale_response_not_rebased_or_applied(self):
        runtime = self.runtime()
        def before(request):
            runtime.move([Op("affect", "energy", "set", 0.4, True)], "изменение во время ожидания")
        def answer(request):
            return Reply(proposals=(rest(cites=refs(request)),), before=before)
        provider = ScriptedProvider([answer, grounded])
        report = ModelLedArchitecture(lambda _: provider).build(runtime).run(runtime.trigger("stale"))
        self.assertEqual(report.outcome, "no_decision")
        self.assertEqual(len(provider.calls), 1)

    def test_echo_not_rewritten_by_adapter(self):
        def answer(request):
            return Reply(proposals=(rest(cites=refs(request)),), extra={"state_version": 0})
        report = self.run_replies(answer)
        self.assertEqual(report["outcome"], "rejected")
        self.assertEqual(report["reason"], "echo_mismatch")

    def test_provider_cannot_mutate_original_request(self):
        request = {"context": [{"ref": "evt_OriginalRef01"}], "private": [1]}
        original = copy.deepcopy(request)
        class Mutating:
            def propose(self, req):
                req.clear()
                return {}
        GroundedProvider(Mutating()).propose(request)
        self.assertEqual(request, original)

    def test_nested_format_retry_configuration_rejected(self):
        runtime = self.runtime()
        with self.assertRaisesRegex(ValueError, "max_format_retries"):
            ModelLedArchitecture(lambda _: SimpleNamespace(max_format_retries=1)).build(runtime)


if __name__ == "__main__":
    unittest.main()
