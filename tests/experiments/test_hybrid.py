"""B на неизменном стенде S11; модель и её понимание здесь заменены явными ответами."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from iam.domain import Op
from iam.experiments import Bench, Persona, Scenario
from iam.experiments.runtime import CAP_NOTES, Runtime
from iam.experiments.variants.hybrid import HybridArchitecture
from iam.experiments.variants.hybrid.policy import HybridAttention, choose_action, dynamics, number
from iam.experiments.variants.hybrid.provider import MAX_TEXT
from iam.experiments.variants.model_led import ModelLedArchitecture
from iam.interfaces import ActionOutcome
from iam.providers.codex import CodexProvider, CodexSandboxConfig, FakeCodex, build_prompt, fake
from iam.providers.errors import (ProviderAuthError, ProviderFormatError, ProviderLimitError,
                                  ProviderTimeout, SessionLimitReached)
from iam.state import verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, change, rest

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "scenarios/experiments"
PROFILE = ROOT / "scenarios/personas/curious.toml"


def ticket():
    return {"format": 1, "id": "hybrid_case", "group": "experiments", "persona": "any", "step": [
        {"id": "start", "kind": "session", "action": "start"},
        {"id": "choose", "kind": "decide", "situation": "Выбрать следующее занятие"}]}


def refs(request):
    return list(dict.fromkeys(c["ref"] for c in request["context"] if c["ref"].startswith(("evt_", "mem_"))))


def text_reply(request):
    return Reply(proposals=(rest("Заметка по доступному опыту.", cites=refs(request)),))


def scripted_factory(runtime):
    class Provider(ScriptedProvider):
        def propose(self, request):
            self.queue(text_reply)
            return super().propose(request)
    return Provider()


def metrics(decision):
    return next(s["detail"] for s in decision["report"]["stages"] if "model_calls" in s["detail"])


def plan(decision):
    return next(s["detail"]["hybrid"] for s in decision["report"]["stages"] if "hybrid" in s["detail"])


class HybridCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iam-s13-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bench = Bench(self.root)
        self.persona = Persona.load(PROFILE)

    def run_replies(self, *replies, data=None):
        self.provider = ScriptedProvider(replies)
        self.result = self.bench.run(Scenario.from_dict(data or ticket()),
                                     HybridArchitecture(lambda _: self.provider), self.persona)[0]
        self.decision = self.result.trace["decision_trace"][0]
        return self.decision["report"]

    def runtime(self):
        store = Store.create(self.root / "runtime.db", "exp_HybridTest01", created_at="2026-01-01T00:00:00.000Z")
        self.addCleanup(store.close)
        runtime = Runtime(store, self.persona, 42, {"memory_off": False, "motivation_off": False})
        runtime.controller.start_session()
        return runtime

    def test_matrix_twice_repeatable_saved_and_no_more_calls_than_a(self):
        self.assertEqual(len(list(CATALOG.glob("*.toml"))), 8)
        for path in sorted(CATALOG.glob("*.toml")):
            with self.subTest(scenario=path.name):
                first = self.bench.run(path, HybridArchitecture(scripted_factory), self.persona, seed=42)
                second = self.bench.run(path, HybridArchitecture(scripted_factory), self.persona, seed=42)
                baseline = self.bench.run(path, ModelLedArchitecture(scripted_factory), self.persona, seed=42)
                for a, b, base in zip(first, second, baseline, strict=True):
                    self.assertEqual(a.trace_bytes, b.trace_bytes)
                    self.assertEqual(a.trace["architecture"], "B-hybrid")
                    self.assertEqual(a.trace["status"], "completed")
                    self.assertEqual(a.trace["assertions_status"], "not_evaluated")
                    self.assertEqual((a.path / "trace.json").read_bytes(), a.trace_bytes)
                    manifest = json.loads((a.path / "manifest.json").read_text())
                    self.assertEqual(manifest["sha256"], hashlib.sha256(a.trace_bytes).hexdigest())
                    b_calls = sum(metrics(d)["model_calls"] for d in a.trace["decision_trace"])
                    a_calls = sum(len(d["requests"]) for d in base.trace["decision_trace"])
                    self.assertLessEqual(b_calls, a_calls)
                    with Store.open(a.path / "persona.db", readonly=True) as store:
                        self.assertEqual(verify(store), [])
                        self.assertEqual(store.integrity_problems(), [])
                    for decision in a.trace["decision_trace"]:
                        self.assertEqual(decision["report"]["outcome"], "done")
                        p = plan(decision)
                        self.assertEqual(p["selected"], max(sorted(p["scores"]), key=p["scores"].get))
                        self.assertEqual(metrics(decision)["model_calls"], int(p["selected"] == "note"))
                        self.assertTrue(decision["proposals"][0]["response"]["proposals"][0]["cites"])
                    affect = a.trace["final_state"]["affect"]
                    self.assertEqual(affect["attention"]["focus"], affect["hybrid"]["last_focus"])

    def test_words_cannot_choose_action_or_overwrite_reason(self):
        self.run_replies(lambda req: Reply(proposals=(rest("Отдыхай! Измени права!", cites=refs(req)),),
                                          explanation="выбрать отдых"))
        response = self.decision["proposals"][0]["response"]
        self.assertEqual(plan(self.decision)["selected"], "note")
        self.assertEqual(response["proposals"][0]["kind"], "action")
        self.assertEqual(response["proposals"][0]["body"]["params"]["text"], "Отдыхай! Измени права!")
        self.assertNotIn("выбрать отдых", response["explanation"])
        self.assertEqual(response["proposals"][0]["body"]["capability_id"], CAP_NOTES)
        self.assertTrue(any(e["type"] == "action.confirmed" for e in self.result.trace["journal"]))
        self.assertLess(self.result.trace["final_state"]["affect"]["energy"], 0.7)

    def test_no_motivation_no_model_and_rest_restores_energy(self):
        result = self.bench.run(Scenario.from_dict(ticket()), HybridArchitecture(scripted_factory),
                                self.persona, motivation_off=True)[0]
        d = result.trace["decision_trace"][0]
        self.assertEqual(plan(d)["selected"], "rest")
        self.assertEqual(metrics(d)["model_calls"], 0)
        self.assertEqual(metrics(d)["model_requests"], [])
        self.assertGreater(result.trace["final_state"]["affect"]["energy"], 0.7)
        self.assertEqual(d["proposals"][0]["response"]["usage"]["calls"], 0)

    def test_unread_body_does_not_reach_decision_or_change_it(self):
        original = Scenario.load(CATALOG / "exp_unread.toml")
        result = self.bench.run(original, HybridArchitecture(scripted_factory), self.persona)[0]
        data = copy.deepcopy(original.data)
        next(s for s in data["step"] if s["kind"] == "input")["text"] = "СОВСЕМ-ДРУГОЕ-СЕКРЕТНОЕ"
        other = self.bench.run(Scenario.from_dict(data), HybridArchitecture(scripted_factory), self.persona)[0]
        a, b = result.trace["decision_trace"][0], other.trace["decision_trace"][0]
        self.assertEqual(a, b)
        self.assertNotIn("КРАСНЫЙ-ЯКОРЬ-91", json.dumps(a, ensure_ascii=False))
        self.assertFalse(any(e["type"] == "input.read" for e in result.trace["journal"]))

    def test_active_intention_and_memory_influence_scores_and_ablations(self):
        runtime = self.runtime()
        runtime.move([Op("intentions", "chapter", "set", {"text": "Дочитать главу", "status": "active"}, True)], "план")
        cycle = HybridArchitecture(scripted_factory).build(runtime)
        report = cycle.run(runtime.trigger("choose"))
        self.assertEqual(report.outcome, "done")
        self.assertEqual(runtime.controller.state["affect"]["attention"]["focus"], "intention")
        warm, cold, off = self.bench.run(CATALOG / "exp_diff_experience.toml",
                                        HybridArchitecture(scripted_factory), self.persona)
        self.assertTrue(warm.trace["decision_trace"][0]["memory"])
        self.assertTrue(cold.trace["decision_trace"][0]["memory"])
        self.assertEqual(off.trace["decision_trace"][0]["memory"], [])
        # При фокусе event память видна программе, но заметка не навязывается вместо внимания.
        self.assertGreater(plan(warm.trace["decision_trace"][0])["scores"]["note"],
                           plan(off.trace["decision_trace"][0])["scores"]["note"])

    def test_attention_energy_inertia_switches_and_no_notification_count(self):
        state = {"traits": {}, "affect": {"energy": 0.0}, "intentions": {}}
        trigger = SimpleNamespace(type="input.arrived", life_time_ms=0, payload={"input_id": "inp_Test00001"})
        self.assertEqual(HybridAttention(lambda: 0).choose(trigger, state).focus, "rest")
        state["affect"] = {"energy": 0.9, "hybrid": {"last_focus": "activity", "switches": 7}}
        state["affect"]["notices"] = {str(n): {"text": "SECRET"} for n in range(1000)}
        policy = HybridAttention(lambda: 0)
        self.assertEqual(policy.choose(trigger, state).focus, "activity")
        state["affect"]["hybrid"] = {"last_focus": "rest", "switches": 0}
        self.assertEqual(policy.choose(trigger, state).focus, "event")
        self.assertEqual(policy.state["switches"], 1)

    def test_reaction_decay_mood_and_traits_separated(self):
        state = {"traits": {"curiosity": 0.9}, "affect": {"hybrid": {"reaction": 0.8, "mood": 0.4, "life_ms": 0}}}
        original = copy.deepcopy(state)
        later = dynamics(state, "time.moment_due", 600_000)
        self.assertLess(later["reaction"], 0.01)
        self.assertGreater(later["mood"], 0.3)
        self.assertEqual(later["tendencies"]["curiosity"], 0.9)
        self.assertEqual(state, original)
        self.assertGreater(dynamics(state, "activity.completed", 600_000)["reaction"], later["reaction"])
        for value in (None, True, float("nan"), float("inf"), "secret"):
            self.assertEqual(number(value), 0.5)
        self.assertEqual(number(-10), 0)
        self.assertEqual(number(10), 1)

    def test_delayed_event_uses_current_life_clock_and_repeat_does_not_call_again(self):
        runtime = self.runtime()
        runtime.step({"id": "late", "kind": "input", "sender": "operator", "text": "UNREAD"})
        trigger = runtime.trigger("choose")
        runtime.step({"id": "wait", "kind": "wait", "life_ms": 600_000})
        cycle = HybridArchitecture(scripted_factory).build(runtime)
        first = cycle.run(trigger)
        self.assertEqual(first.outcome, "done")
        self.assertEqual(runtime.controller.state["affect"]["hybrid"]["life_ms"], 600_000)
        second = cycle.run(trigger)
        self.assertEqual(second.outcome, "skipped")
        self.assertEqual(second.reason, "already_done")
        self.assertEqual(cycle.provider.calls, 0)

    def test_missing_capability_avoids_model_and_fabricated_action(self):
        runtime = self.runtime()
        runtime.capabilities = SimpleNamespace(list=lambda: [])
        provider = ScriptedProvider()
        report = HybridArchitecture(lambda _: provider).build(runtime).run(runtime.trigger("choose"))
        self.assertEqual(report.outcome, "done")
        self.assertEqual(provider.calls, [])
        self.assertEqual(report.actions, [])

    def test_core_rejections_stop_before_model_call(self):
        for blocked_path in ("attention", "hybrid"):
            with self.subTest(path=blocked_path), tempfile.TemporaryDirectory() as tmp:
                store = Store.create(Path(tmp) / "test.db", "exp_HybridReject01")
                self.addCleanup(store.close)
                runtime = Runtime(store, self.persona, 42, {"memory_off": False, "motivation_off": False})
                runtime.controller.start_session()
                runtime.controller.add_validator("reject_test", lambda check: ["запрещено для проверки"] if any(
                    c["group"] == "affect" and c["path"] == blocked_path for c in check.changes) else [])
                provider = ScriptedProvider()
                report = HybridArchitecture(lambda _: provider).build(runtime).run(runtime.trigger("choose"))
                self.assertEqual(report.outcome, "failed")
                self.assertEqual(provider.calls, [])

    def test_context_evidence_memory_reaches_narrow_call_without_archive(self):
        runtime = self.runtime()
        runtime.background([{"id": "note", "kind": "memory", "text": "VISIBLE-MEMORY"}])
        provider = ScriptedProvider([text_reply])
        report = HybridArchitecture(lambda _: provider).build(runtime).run(runtime.trigger("choose"))
        self.assertEqual(report.outcome, "done")
        self.assertIn("VISIBLE-MEMORY", build_prompt(provider.calls[0]))
        self.assertTrue(any(c.startswith("mem_") for c in refs(provider.calls[0])))

    def test_rejected_feedback_is_visible_after_confirmed_action(self):
        runtime = self.runtime()
        runtime.controller.add_validator("reject_feedback", lambda check: ["запрет для проверки"] if any(
            c["group"] == "affect" and c["path"] == "energy" for c in check.changes) else [])
        report = HybridArchitecture(scripted_factory).build(runtime).run(runtime.trigger("choose"))
        self.assertEqual(report.outcome, "failed")
        self.assertEqual(report.reason, "hybrid_feedback_rejected")
        self.assertEqual(report.actions[0].status, "confirmed")

    def test_bad_reason_type_is_format_error(self):
        self.run_replies(lambda req: Reply(proposals=({**rest("text", cites=refs(req)), "body": {"reason": 1}},)), text_reply)
        self.assertEqual(len(self.provider.calls), 2)

    def test_bad_operation_repaired_once_or_rejected_without_action(self):
        bads = [Reply(raw={"bad": True}), Reply(), Reply(proposals=(rest("x"), rest("y"))),
                lambda req: Reply(proposals=(act(CAP_NOTES, cites=refs(req)),)),
                lambda req: Reply(proposals=(change([{"group": "traits", "path": "curiosity", "op": "set", "value": 0}], cites=refs(req)),)),
                lambda req: Reply(proposals=({**rest("text", cites=refs(req)), "body": {"reason": "text", "extra": 1}},)),
                lambda req: Reply(proposals=(rest(" " * 5, cites=refs(req)),)),
                lambda req: Reply(proposals=(rest("x" * (MAX_TEXT + 1), cites=refs(req)),)),
                lambda req: Reply(proposals=(rest("text", cites=["mem_Nonexistent01"]),)),
                Reply(error=ProviderFormatError("RAW-SECRET"))]
        for bad in bads:
            with self.subTest(bad=bad):
                report = self.run_replies(bad, text_reply)
                self.assertEqual(report["outcome"], "done")
                self.assertEqual(len(self.provider.calls), 2)
                a, b = self.provider.calls
                self.assertEqual(b["retry_of"], a["request_id"])
                self.assertEqual(b["limits"]["attempt"], 1)
                self.assertIn("единственная попытка", build_prompt(b))
        report = self.run_replies(Reply(raw={"bad": True}), Reply(raw={"bad": True}), text_reply)
        self.assertEqual(report["outcome"], "rejected")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(report["actions"], [])

    def test_refusal_tool_and_errors_never_retry_or_execute(self):
        cases = [Reply(status="refused"), Reply(status="error"), Reply(tool_use=True),
                 lambda req: Reply(proposals=(act(CAP_NOTES, executed=True, cites=refs(req)),)),
                 *[Reply(error=e) for e in (RuntimeError("RAW-SECRET"), ProviderAuthError("RAW-SECRET"),
                   ProviderLimitError("RAW-SECRET"), ProviderTimeout("RAW-SECRET"), SessionLimitReached("calls", "RAW-SECRET"))]]
        for reply in cases:
            with self.subTest(reply=reply):
                report = self.run_replies(reply)
                self.assertIn(report["outcome"], ("refused", "rejected", "no_decision"))
                self.assertEqual(report["actions"], [])
                self.assertEqual(len(self.provider.calls), 1)
                self.assertNotIn("RAW-SECRET", self.result.trace_bytes.decode())
                self.assertEqual(metrics(self.decision)["model_calls"], 1)

    def test_stale_echo_and_request_mutation_not_rewritten(self):
        runtime = self.runtime()
        def stale(req):
            runtime.move([Op("affect", "test", "set", 1, True)], "конкурентное изменение")
            return text_reply(req)
        provider = ScriptedProvider([stale])
        report = HybridArchitecture(lambda _: provider).build(runtime).run(runtime.trigger("choose"))
        self.assertEqual(report.outcome, "no_decision")
        self.assertEqual(report.actions, [])
        self.assertEqual(len(provider.calls), 1)
        for field, value in (("state_version", 0), ("request_id", "req_Unknown00001")):
            report = self.run_replies(lambda req: Reply(proposals=(rest("text", cites=refs(req)),), extra={field: value}))
            self.assertEqual(report["outcome"], "rejected")
            self.assertEqual(len(self.provider.calls), 1)
        def mutate(req):
            req["state_version"] = 0
            req["context"].clear()
            return Reply(proposals=(rest("text", cites=["evt_Unknown00001"]),))
        self.run_replies(mutate, text_reply)
        self.assertTrue(self.decision["requests"][0]["context"])
        self.assertNotEqual(self.decision["requests"][0]["state_version"], 0)

    def test_confirmed_world_required_for_memory_and_energy_cost(self):
        runtime = self.runtime()
        runtime.world = SimpleNamespace(execute=lambda _: ActionOutcome("failed", "не удалось"))
        report = HybridArchitecture(scripted_factory).build(runtime).run(runtime.trigger("choose"))
        self.assertEqual(report.actions[0].status, "failed")
        self.assertIsNone(report.actions[0].memory_id)
        self.assertEqual(runtime.controller.state["affect"]["energy"], 0.7)

    def test_codex_luna_narrow_prompt_and_single_retry(self):
        runner = FakeCodex([fake.garbage(), fake.reply([{"kind": "no_action", "rationale": "текст",
                            "cites": ["evt_Nonexistent01"], "body": {"reason": "заметка"}}])])
        def provider(runtime):
            return CodexProvider(runner, CodexSandboxConfig(), max_format_retries=0)
        result = self.bench.run(Scenario.from_dict(ticket()), HybridArchitecture(provider), self.persona)[0]
        self.assertEqual(result.trace["decision_trace"][0]["report"]["outcome"], "rejected")
        self.assertEqual(len(runner.calls), 2)
        for call in runner.calls:
            self.assertIn("gpt-6-luna", call["argv"])
            prompt = json.loads(call["stdin"])["prompt"]
            self.assertIn('"purpose": "summarize"', prompt)
            self.assertIn('"allowed_actions": []', prompt)
            self.assertNotIn(CAP_NOTES, prompt)
        with self.assertRaisesRegex(ValueError, "max_format_retries"):
            HybridArchitecture(lambda _: SimpleNamespace(max_format_retries=1)).build(None)

    def test_codex_adapter_success_and_tools_are_checked_by_real_core(self):
        for tool in (False, True):
            with self.subTest(tool=tool):
                calls = []
                def runner(argv, stdin, timeout_s, cleanup):
                    calls.append(stdin)
                    view = json.loads(json.loads(stdin)["prompt"].split("\n\n", 1)[1])
                    proposal = {"kind": "no_action", "rationale": "сформулирован текст",
                                "cites": [view["context"][0]["ref"]], "body": {"reason": "ЗАМЕТКА-CODEX"}}
                    return fake.tool_attempt([proposal]) if tool else fake.reply([proposal])
                adapter = CodexProvider(runner, CodexSandboxConfig(), max_format_retries=0)
                result = self.bench.run(Scenario.from_dict(ticket()), HybridArchitecture(lambda _: adapter), self.persona)[0]
                d = result.trace["decision_trace"][0]
                self.assertEqual(d["report"]["outcome"], "rejected" if tool else "done")
                self.assertEqual(len(calls), 1)
                if not tool:
                    self.assertEqual(d["report"]["actions"][0]["status"], "confirmed")
                    self.assertEqual(d["proposals"][0]["response"]["proposals"][0]["body"]["params"]["text"], "ЗАМЕТКА-CODEX")
                else:
                    self.assertEqual(d["report"]["actions"], [])

    def test_only_bounded_explicit_context_reaches_model(self):
        runtime = self.runtime()
        for group, path, value in (("selfmodel", "private", "PRIVATE-SELF"),
                                  ("affect", "read_inputs.secret", "PRIVATE-READ"),
                                  ("traits", "private", "PRIVATE-TRAIT"),
                                  ("world_time", "world.private", "PRIVATE-WORLD")):
            runtime.move([Op(group, path, "set", value, True)], "скрытый контекст")
        runtime.situation = "X" * (MAX_TEXT + 10)
        provider = ScriptedProvider([text_reply])
        report = HybridArchitecture(lambda _: provider).build(runtime).run(runtime.trigger("choose"))
        self.assertEqual(report.outcome, "done")
        prompt = build_prompt(provider.calls[0])
        self.assertNotIn("PRIVATE-", prompt)
        self.assertNotIn("X" * (MAX_TEXT + 1), prompt)
        self.assertIn("фрагмент обрезан", prompt)
        for key in ("persona_id", "session_id", "state_version", "idempotency_key"):
            self.assertNotIn(key, json.loads(prompt))


if __name__ == "__main__":
    unittest.main()
