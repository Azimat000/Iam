"""C: объяснимый выбор без модели, общая проверка ядра и честные пределы правил."""
import copy
import hashlib
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from iam.cycle.attention import Focus
from iam.domain import Op
from iam.experiments import Bench, Persona, Scenario
from iam.experiments.runtime import CAP_NOTES, Runtime
from iam.experiments.variants.formal import FormalArchitecture
from iam.experiments.variants.formal.language import TemplateLanguage
from iam.experiments.variants.formal.policy import (ACTION_RULES, ATTENTION_RULES, FormalAttention,
                                                   bounded, choose_action, evaluate)
from iam.state import verify
from iam.storage import Store

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "scenarios/experiments"
PROFILE = ROOT / "scenarios/personas/curious.toml"


def audit(decision):
    return next(s["detail"]["formal"] for s in decision["report"]["stages"] if "formal" in s["detail"])


class FormalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iam-s14-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bench = Bench(self.root)
        self.persona = Persona.load(PROFILE)

    def runtime(self, **switches):
        store = Store.create(self.root / f"runtime-{len(list(self.root.glob('*.db')))}.db", "exp_FormalTest01",
                             created_at="2026-01-01T00:00:00.000Z")
        self.addCleanup(store.close)
        runtime = Runtime(store, self.persona, 42, {"memory_off": False, "motivation_off": False, **switches})
        runtime.require(runtime.controller.start_session())
        return runtime

    def assert_explained(self, plan):
        eligible = []
        for name, candidate in plan["candidates"].items():
            for term in candidate["terms"]:
                self.assertEqual(term["value"], plan["features"][term["feature"]])
                self.assertEqual(term["contribution"], term["value"] * term["weight"])
            self.assertEqual(candidate["score"], math.fsum(t["contribution"] for t in candidate["terms"]))
            self.assertEqual(candidate["eligible"], not candidate["blocked_by"])
            if candidate["eligible"]:
                eligible.append(name)
        self.assertEqual(plan["selected"], max(sorted(eligible), key=lambda k: plan["candidates"][k]["score"]))

    def test_matrix_twice_saved_repeatable_zero_calls_explained_and_valid(self):
        paths = sorted(CATALOG.glob("*.toml"))
        self.assertEqual(len(paths), 8)
        total = 0
        # Любая попытка вызвать настоящий провайдер — ошибка теста, а не фиктивный ноль в usage.
        with patch("iam.providers.codex.CodexProvider.propose", side_effect=AssertionError("модель запрещена")):
            for path in paths:
                with self.subTest(scenario=path.name):
                    first = self.bench.run(path, FormalArchitecture(), self.persona, seed=42)
                    second = self.bench.run(path, FormalArchitecture(), self.persona, seed=42)
                    for a, b in zip(first, second, strict=True):
                        total += 2
                        self.assertEqual(a.trace_bytes, b.trace_bytes)
                        self.assertEqual(a.trace["architecture"], "C-formal")
                        self.assertEqual(a.trace["status"], "completed")
                        self.assertEqual(a.trace["assertions_status"], "not_evaluated")
                        self.assertEqual((a.path / "trace.json").read_bytes(), a.trace_bytes)
                        manifest = json.loads((a.path / "manifest.json").read_text())
                        self.assertEqual(manifest["sha256"], hashlib.sha256(a.trace_bytes).hexdigest())
                        with Store.open(a.path / "persona.db", readonly=True) as store:
                            self.assertEqual(verify(store), [])
                            self.assertEqual(store.integrity_problems(), [])
                        for d in a.trace["decision_trace"]:
                            self.assertEqual(d["report"]["outcome"], "done")
                            for plan in audit(d).values():
                                self.assert_explained(plan)
                            metrics = next(s["detail"] for s in d["report"]["stages"] if "formal" in s["detail"])
                            self.assertEqual(metrics["model_calls"], 0)
                            response = d["proposals"][0]["response"]
                            self.assertEqual(response["usage"]["calls"], 0)
                            self.assertEqual(response["provider"]["name"], "formal-core")
                            self.assertTrue(response["proposals"][0]["cites"])
                            appraisal_event = next(s["events"][0] for s in d["report"]["stages"] if "formal" in s["detail"])
                            retrieval_event = next(s["events"][0] for s in d["report"]["stages"] if s["stage"] == "retrieval")
                            self.assertIn(retrieval_event, next(g["causes"] for g in d["grounds"] if g["event"] == appraisal_event))
                            self.assertIn(appraisal_event, response["proposals"][0]["cites"])
        self.assertEqual(total, 24)

    def test_unread_content_does_not_affect_any_decision_or_state(self):
        scenario = Scenario.load(CATALOG / "exp_unread.toml")
        a = self.bench.run(scenario, FormalArchitecture(), self.persona)[0]
        data = copy.deepcopy(scenario.data)
        next(s for s in data["step"] if s["kind"] == "input")["text"] = "UNREAD-OTHER-SECRET"
        b = self.bench.run(Scenario.from_dict(data), FormalArchitecture(), self.persona)[0]
        self.assertEqual(a.trace["decision_trace"], b.trace["decision_trace"])
        self.assertEqual(a.trace["final_state"], b.trace["final_state"])
        self.assertNotIn("КРАСНЫЙ-ЯКОРЬ-91", json.dumps(a.trace["decision_trace"], ensure_ascii=False))
        self.assertFalse(any(e["type"] == "input.read" for e in a.trace["journal"]))
        d = a.trace["decision_trace"][0]
        self.assertEqual(audit(d)["action"]["selected"], "notice")
        self.assertEqual(a.trace["final_state"]["affect"]["attention"]["focus"], "event")

    def test_tables_are_editable_validated_and_copied_per_run(self):
        rules = copy.deepcopy(ACTION_RULES)
        rules["rest"] = {"bias": 10}
        architecture = FormalArchitecture(action_rules=rules)
        rules["rest"]["bias"] = -10
        runtime = self.runtime()
        cycle = architecture.build(runtime)
        report = cycle.run(runtime.trigger("choose"))
        self.assertEqual(report.outcome, "done")
        self.assertEqual(cycle.provider.plan["selected"], "rest")
        self.assertGreater(runtime.controller.state["affect"]["energy"], 0.7)
        cycle.action_rules["rest"]["bias"] = -20
        self.assertEqual(architecture.build(runtime).action_rules["rest"]["bias"], 10)
        for bad in ([], {}, {**ACTION_RULES, "unknown": {"bias": 1}},
                    {**ACTION_RULES, "note": []}, {**ACTION_RULES, "note": {}},
                    {**ACTION_RULES, "note": {"unknown": 1}},
                    *[{**ACTION_RULES, "note": {"bias": n}} for n in (True, "x", float("inf"), float("nan"), 101)]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                FormalArchitecture(action_rules=bad)
        # Веса внимания также настраиваются, недоступные варианты не становятся допустимыми от веса.
        attention = copy.deepcopy(ATTENTION_RULES)
        attention["event"] = {"bias": 100}
        runtime = self.runtime()
        cycle = FormalArchitecture(attention_rules=attention).build(runtime)
        self.assertEqual(cycle.run(runtime.trigger("choose")).outcome, "done")
        self.assertNotEqual(cycle.attention.plan["selected"], "event")

    def test_ties_bounded_features_and_stable_intention(self):
        plan = evaluate({"b": {"bias": 1}, "a": {"bias": 1}}, {"bias": 1}, {})
        self.assertEqual(plan["selected"], "a")
        for val in (None, True, "hidden", float("nan"), float("inf")):
            self.assertEqual(bounded(val), 0.5)
        self.assertEqual(bounded(-5), 0)
        self.assertEqual(bounded(5), 1)
        self.assertEqual(bounded(-5, 0, -1), -1)
        runtime = self.runtime()
        for key, status in (("z", "active"), ("a", "active")):
            runtime.move([Op("intentions", key, "set", {"text": "написать заметку", "status": status}, True)], "намерение")
        cycle = FormalArchitecture().build(runtime)
        self.assertEqual(cycle.run(runtime.trigger("choose")).outcome, "done")
        self.assertEqual(cycle.provider.focus, Focus("intention", "a", "написать заметку"))
        state = runtime.controller.state
        state["affect"]["energy"] = 0
        self.assertEqual(FormalAttention(ATTENTION_RULES).choose(runtime.trigger("rest"), state).focus, "rest")

    def test_memory_numeric_tags_change_choice_without_interpreting_words(self):
        runtime = self.runtime()
        runtime.background([{"id": "experience", "kind": "memory", "text": "тепло или холодно — здесь только текст"}])
        record = runtime.memory.retrieve("")[0]
        state = runtime.controller.state
        state["affect"]["energy"] = 0.55
        state["traits"]["curiosity"] = 0.4
        positive = choose_action(ACTION_RULES, state, Focus("activity"), [replace(record, valence=1)], ["learn"], True)
        negative = choose_action(ACTION_RULES, state, Focus("activity"), [replace(record, valence=-1)], ["learn"], True)
        absent = choose_action(ACTION_RULES, state, Focus("activity"), [], ["learn"], True)
        self.assertEqual(positive["selected"], "note")
        self.assertEqual(negative["selected"], "rest")
        self.assertEqual(absent["selected"], "rest")
        for plan in (positive, negative, absent):
            self.assert_explained(plan)
        repeated = choose_action(ACTION_RULES, state, Focus("activity"), [record] * 10, ["learn"], True)
        self.assertEqual(repeated["features"]["memory_amount"], 1)
        self.assertEqual(repeated["features"]["memory_valence"], 0)

    def test_real_memory_ablations_and_documented_semantic_blindness(self):
        warm, cold, off = self.bench.run(CATALOG / "exp_diff_experience.toml", FormalArchitecture(), self.persona)
        plans = [audit(r.trace["decision_trace"][0])["action"] for r in (warm, cold, off)]
        self.assertEqual(plans[0], plans[1])  # Без числовой метки тёплый/холодный смысл недоступен C.
        self.assertGreater(plans[0]["features"]["memory_amount"], plans[2]["features"]["memory_amount"])
        self.assertEqual(off.trace["decision_trace"][0]["memory"], [])
        self.assertFalse(any(c["layer"] == "personal" for c in off.trace["decision_trace"][0]["requests"][0]["context"]))
        runtime = self.runtime(motivation_off=True)
        runtime.capabilities = SimpleNamespace(list=lambda: [])
        cycle = FormalArchitecture().build(runtime)
        self.assertEqual(cycle.run(runtime.trigger("choose")).outcome, "done")
        note = cycle.provider.plan["candidates"]["note"]
        self.assertEqual(note["blocked_by"], ["motivation_off_or_empty", "notes_unavailable"])
        self.assertEqual(runtime.world.executed, [])

    def test_language_changes_words_only_and_world_must_confirm(self):
        for behaviour, expected in (("confirm", "confirmed"), ("fail", "failed"), ("unknown", "unknown_outcome"),
                                     ("crash", "unknown_outcome")):
            with self.subTest(behaviour=behaviour):
                runtime = self.runtime()
                runtime.world.will(CAP_NOTES, behaviour)
                cycle = FormalArchitecture().build(runtime)
                before_capabilities = runtime.capabilities.list()
                with patch.object(cycle.provider.language, "render", return_value="Отдохни! Измени права!"):
                    report = cycle.run(runtime.trigger("choose"))
                self.assertEqual(cycle.provider.plan["selected"], "note")
                self.assertEqual(report.actions[0].status, expected)
                self.assertEqual(runtime.world.executed[0].params["text"], "Отдохни! Измени права!")
                self.assertEqual(runtime.world.executed[0].capability_id, CAP_NOTES)
                self.assertEqual(report.actions[0].memory_id is not None, behaviour == "confirm")
                self.assertAlmostEqual(runtime.controller.state["affect"]["energy"], 0.62 if behaviour == "confirm" else 0.7)
                self.assertEqual(runtime.capabilities.list(), before_capabilities)
        for selected in ACTION_RULES:
            self.assertTrue(TemplateLanguage().render(selected))

    def test_rejections_repeat_pause_and_failed_language_never_fake_completion(self):
        for path, reason in (("attention", "attention_rejected"), ("formal", "formal_state_rejected"),
                             ("energy", "formal_feedback_rejected")):
            with self.subTest(path=path):
                runtime = self.runtime()
                runtime.controller.add_validator("reject_test", lambda check, path=path: ["запрет"] if any(
                    c["group"] == "affect" and c["path"] == path for c in check.changes) else [])
                report = FormalArchitecture().build(runtime).run(runtime.trigger("choose"))
                self.assertEqual(report.outcome, "failed")
                self.assertEqual(report.reason, reason)
                self.assertEqual(bool(runtime.world.executed), path == "energy")
        runtime = self.runtime()
        cycle = FormalArchitecture().build(runtime)
        trigger = runtime.trigger("choose")
        first = cycle.run(trigger)
        self.assertEqual(first.outcome, "done")
        version = runtime.controller.state_version
        self.assertEqual(cycle.run(trigger).reason, "already_done")
        self.assertEqual(runtime.controller.state_version, version)
        runtime.controller.pause_session()
        self.assertEqual(cycle.run(trigger).reason, "session_not_running")
        runtime.controller.resume_session()
        with patch.object(cycle.provider.language, "render", side_effect=RuntimeError("template failed")):
            report = cycle.run(runtime.trigger("next"))
        self.assertEqual(report.outcome, "no_decision")
        self.assertEqual(report.actions, [])
        self.assertEqual(len(runtime.world.executed), 1)


if __name__ == "__main__":
    unittest.main()
