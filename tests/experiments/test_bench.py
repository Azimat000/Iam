"""Приёмка стенда: реальные SQLite/ядро/цикл, учебный выбор без сетевых обращений."""
import copy
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from iam.domain import MemoryRecord, Source, new_id
from iam.experiments import Bench, Persona, Results, Scenario
from iam.experiments.__main__ import main
from iam.experiments.results import Normalizer
from iam.experiments.toy import ToyArchitecture
from iam.interfaces import ActionOutcome, ActionRequest, Memory, World
from iam.state import StateController
from iam.storage import Store
from iam.storage.errors import PersonaMismatch
from iam.stubs import Reply, ScriptedProvider, act
from iam.experiments.runtime import CAP_NOTES

PROFILES = ROOT / "scenarios/personas"
CATALOG = ROOT / "scenarios/experiments"


def ticket(decisions=1, **extra):
    return {"format": 1, "id": "test_case", "group": "experiments", "persona": "any",
            "step": [{"id": "start", "kind": "session", "action": "start"}] +
                    [{"id": f"choose_{i}", "kind": "decide", "situation": "Выбрать занятие"} for i in range(decisions)],
            **extra}


class BenchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iam-s11-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bench = Bench(self.root)
        self.persona = Persona.load(PROFILES / "curious.toml")

    def run_ticket(self, data=None, **kw):
        return self.bench.run(Scenario.from_dict(data or ticket()), ToyArchitecture(), self.persona, **kw)

    def test_identical_seed_identical_bytes_for_all_catalog_runs_and_profiles(self):
        # Отдельные файлы и случайные технические ID; сравниваем всю трассу, не только решение.
        for profile in sorted(PROFILES.glob("*.toml")):
            for scenario in sorted(CATALOG.glob("*.toml")):
                with self.subTest(profile=profile.stem, scenario=scenario.stem):
                    a = self.bench.run(scenario, ToyArchitecture(), profile, seed=917)
                    b = self.bench.run(scenario, ToyArchitecture(), profile, seed=917)
                    for first, second in zip(a, b, strict=True):
                        self.assertEqual(first.trace_bytes, second.trace_bytes)
                        self.assertNotEqual(first.path, second.path)
                        self.assertEqual(first.trace["assertions_status"], "not_evaluated")
                        self.assertEqual(first.trace["status"], "completed")
                        self.assertTrue(all(d["report"]["outcome"] == "done" for d in first.trace["decision_trace"]))

    def test_motivation_catalog_executes(self):
        for scenario in sorted((ROOT / "scenarios/motivation").glob("*.toml")):
            for result in self.bench.run(scenario, ToyArchitecture(), self.persona, seed=2):
                self.assertEqual(result.trace["status"], "completed")
                self.assertLessEqual(result.trace["decisions"], 4)

    def test_memory_ablation_changes_actual_proposal_and_removes_past_from_requests(self):
        warm, cold, off = self.bench.run(CATALOG / "exp_diff_experience.toml", ToyArchitecture(), self.persona)
        def proposal(result):
            return result.trace["decision_trace"][0]["proposals"]
        self.assertNotEqual(proposal(warm), proposal(cold))
        self.assertNotEqual(proposal(warm), proposal(off))
        self.assertTrue(warm.trace["decision_trace"][0]["memory"])
        self.assertEqual(off.trace["decision_trace"][0]["memory"], [])
        requests = json.dumps(off.trace["decision_trace"][0]["requests"], ensure_ascii=False)
        self.assertNotIn("тепло и долго", requests)
        # Опыт существует в технической базе, но при отключении не извлекается и не отправляется.
        with Store.open(off.path / "persona.db", readonly=True) as store:
            self.assertEqual(len(store.search_memory("тепло")), 1)

    def test_motivation_ablation_changes_action_to_no_action(self):
        (on,) = self.run_ticket()
        (off,) = self.run_ticket(motivation_off=True)
        on_trace, off_trace = on.trace["decision_trace"][0], off.trace["decision_trace"][0]
        self.assertEqual(on_trace["report"]["actions"][0]["status"], "confirmed")
        self.assertEqual(off_trace["report"]["actions"], [])
        self.assertEqual(off_trace["motives"], [])
        for motive in self.persona.motives:
            self.assertNotIn(motive, json.dumps(off_trace["requests"], ensure_ascii=False))
        self.assertNotEqual(on_trace["proposals"], off_trace["proposals"])

    def test_four_decision_limit_is_per_run_and_later_steps_are_not_executed(self):
        sc = ticket(5, run=[{"id": "first"}, {"id": "second"}])
        sc["step"].append({"id": "too_late", "kind": "input", "sender": "operator", "text": "НЕ ВЫПОЛНЯТЬ"})
        for result in self.run_ticket(sc):
            self.assertEqual(result.trace["status"], "decision_limit")
            self.assertEqual(result.trace["decisions"], 4)
            self.assertEqual(len(result.trace["decision_trace"]), 4)
            self.assertNotIn("НЕ ВЫПОЛНЯТЬ", result.trace_bytes.decode())
            self.assertEqual(result.trace["final_state"]["world_time"]["session_state"], "stopped")

    def test_seed_recorded_and_no_global_random_state(self):
        import random
        before = random.getstate()
        a = self.run_ticket(ticket(4, seed=123))[0]
        self.assertEqual(a.trace["seed"], 123)
        self.assertEqual(before, random.getstate())
        b = self.run_ticket(ticket(4, seed=123), seed=456)[0]
        self.assertEqual(b.trace["seed"], 456)
        self.assertNotEqual(a.trace["decision_trace"], b.trace["decision_trace"])

    def test_unread_content_stays_out_of_context_memory_state_and_actions(self):
        result = self.bench.run(CATALOG / "exp_unread.toml", ToyArchitecture(), self.persona)[0]
        for key in ("decision_trace", "final_state", "snapshots"):
            self.assertNotIn("КРАСНЫЙ-ЯКОРЬ-91", json.dumps(result.trace[key], ensure_ascii=False))
        types = [e["type"] for e in result.trace["journal"]]
        self.assertIn("input.noticed", types)
        self.assertNotIn("input.read", types)
        self.assertIn("КРАСНЫЙ-ЯКОРЬ-91", result.trace_bytes.decode())  # технический журнал знает сырой вход

    def test_separate_databases_and_permanent_identity_refusal(self):
        results = self.run_ticket(ticket(run=[{"id": "one"}, {"id": "two"}]))
        identities = []
        for result in results:
            path = result.path / "persona.db"
            with Store.open(path, readonly=True) as store:
                identities.append(store.persona_id)
                self.assertEqual(store.store_kind, "experimental")
            with self.assertRaises(PersonaMismatch):
                Store.open(path, readonly=True, expect_persona="per_Permanent01")
        self.assertEqual(len(set(identities)), 2)
        self.assertFalse((self.root / "personas").exists())

    def test_simulation_memory_cannot_be_written_to_permanent_personality(self):
        (result,) = self.run_ticket(ticket(background=[{"kind": "memory", "id": "tea", "text": "Опыт симуляции"}]))
        with Store.open(result.path / "persona.db", readonly=True) as experimental:
            record = experimental.search_memory("симуляции")[0]
        with Store.create(self.root / "permanent.db", "per_Permanent01") as store:
            c = StateController(store)
            c.create_persona()
            c.start_session()
            from dataclasses import replace
            from iam.domain import Op, Transition
            from iam.state import Proposal
            rec = replace(record, persona_id=c.persona_id)
            tr = Transition(new_id("trn"), c.persona_id, c.epoch, c.state_version,
                            (Op("memory", rec.memory_id, "add", rec.to_dict(), True),), "попытка переноса",
                            (c.events_after(0)[0].event_id,))
            self.assertEqual(c.apply(Proposal(tr)).violations[0].code, "simulation_in_permanent")

    def test_trace_cites_existing_events_and_saved_digest_matches(self):
        (result,) = self.run_ticket(ticket(3))
        ids = {e["event_id"] for e in result.trace["journal"]}
        for decision in result.trace["decision_trace"]:
            for ground in decision["grounds"]:
                self.assertIn(ground["event"], ids)
                self.assertTrue(set(ground["causes"]) <= ids)
        self.assertEqual((result.path / "trace.json").read_bytes(), result.trace_bytes)
        table = list(csv.DictReader(io.StringIO(self.bench.results.table())))
        self.assertEqual(table[0]["persona"], "curious")
        self.assertEqual(table[0]["decisions"], "3")
        (result.path / "trace.json").write_text("подмена", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "повреждена"):
            self.bench.results.table()

    def test_existing_directory_and_incomplete_run_are_preserved(self):
        path = self.bench.results.reserve()
        marker = path / "keep.txt"
        marker.write_text("старый результат")
        (result,) = self.run_ticket()
        self.assertNotEqual(result.path, path)
        self.assertEqual(marker.read_text(), "старый результат")
        self.assertEqual(len(list(csv.DictReader(io.StringIO(self.bench.results.table())))), 1)

    def test_world_fail_unknown_and_calendar_pause(self):
        sc = ticket()
        sc["step"][1:1] = [
            {"id": "pause", "kind": "session", "action": "pause"},
            {"id": "calendar", "kind": "wait", "calendar_ms": 5000},
            {"id": "resume", "kind": "session", "action": "resume"},
            {"id": "time", "kind": "wait", "life_ms": 1000},
            {"id": "f", "kind": "world", "action": "неудача", "outcome": "failed"},
            {"id": "u", "kind": "world", "action": "нет ответа", "outcome": "no_answer"},
        ]
        (result,) = self.run_ticket(sc)
        events = result.trace["journal"]
        self.assertIn("action.failed", [e["type"] for e in events])
        self.assertIn("action.unknown_outcome", [e["type"] for e in events])
        self.assertEqual(result.trace["final_state"]["world_time"]["life_time_ms"], 1000)

    def test_life_wait_in_pause_fails_and_releases_writer(self):
        sc = ticket()
        sc["step"][1:1] = [{"id": "pause", "kind": "session", "action": "pause"},
                            {"id": "wait", "kind": "wait", "life_ms": 10}]
        with self.assertRaisesRegex(ValueError, "в паузе"):
            self.run_ticket(sc)
        path = self.bench.results.root / "run-000001/persona.db"
        # Открываем только чтение: не выполняем миграций и не меняем след неудачного эксперимента.
        with Store.open(path, readonly=True) as store:
            self.assertEqual(store.store_kind, "experimental")
        self.assertEqual(len(list(csv.DictReader(io.StringIO(self.bench.results.table())))), 0)

    def test_wrong_factory_is_rejected(self):
        class Bad:
            name = "bad"
            def build(self, runtime):
                return object()
        with self.assertRaisesRegex(ValueError, "общее ядро"):
            self.bench.run(Scenario.from_dict(ticket()), Bad(), self.persona)

    def test_real_kernel_rejects_unavailable_action(self):
        class Bad:
            name = "bad_action"
            def build(self, runtime):
                return runtime.cycle(ScriptedProvider([Reply(proposals=(act("cap_Unavailable01"),))]))
        (result,) = self.bench.run(Scenario.from_dict(ticket()), Bad(), self.persona)
        types = [e["type"] for e in result.trace["journal"]]
        self.assertIn("proposal.rejected", types)
        self.assertNotIn("action.confirmed", types)

    def test_memory_port_archive_and_off_describe(self):
        captured = []
        class Inspect(ToyArchitecture):
            def build(self, runtime):
                captured.append(runtime)
                self_test.assertIsInstance(runtime.memory, Memory)
                self_test.assertIsInstance(runtime.world, World)
                return super().build(runtime)
        self_test = self
        sc = Scenario.from_dict(ticket())
        self.bench.run(sc, Inspect(), self.persona, memory_off=True)
        mem = captured[-1].memory
        self.assertEqual(mem.retrieve("anything"), [])  # база уже закрыта: off даже её не читает
        action = ActionRequest("act_Example001", "action", CAP_NOTES)
        self.assertIsNone(mem.describe(action, ActionOutcome("confirmed", "итог")))
        self.bench.run(sc, Inspect(), self.persona)
        mem = captured[-1].memory
        self.assertIsNone(mem.describe(action, ActionOutcome("failed", "итог")))
        self.assertIn("выполнила", mem.describe(action, ActionOutcome("confirmed", "итог")))

    def test_read_input_is_explicit_and_uses_kernel(self):
        captured = []
        class Read(ToyArchitecture):
            def build(self, runtime):
                cycle = super().build(runtime)
                original = cycle.run
                def run(trigger):
                    if trigger.type == "input.arrived":
                        captured.append(runtime.read_input(trigger.payload["input_id"]))
                    return original(trigger)
                cycle.run = run
                return cycle
        self.bench.run(CATALOG / "exp_unread.toml", Read(), self.persona)
        self.assertIn("КРАСНЫЙ-ЯКОРЬ-91", captured[0][0])
        self.assertTrue(captured[0][1].startswith("evt_"))

    def test_regular_state_transition_memory_is_retrieved_and_revision_is_current(self):
        captured = []
        self_test = self
        class Write(ToyArchitecture):
            def build(self, runtime):
                from dataclasses import replace
                from iam.domain import Op
                c = runtime.controller
                cause = c.events_after(0)[-1].event_id
                rec = MemoryRecord("mem_Ordinary001", c.persona_id, "personal", "semantic", "simulation_event",
                                   {"text": "Старое значение"}, Source("core", "fixture"), c.clock.calendar_now(),
                                   c.life_time_ms, (cause,))
                runtime.move([Op("memory", rec.memory_id, "add", rec.to_dict(), True)], "обычная запись")
                self_test.assertEqual(c.events_after(0)[-1].type, "state.transition_applied")
                self_test.assertEqual(runtime.memory.retrieve("")[0].text, "Старое значение")
                updated = replace(rec, version=2, content={"text": "Исправленное значение"})
                runtime.move([Op("memory", rec.memory_id, "set", updated.to_dict(), True)], "исправление записи")
                captured.extend(runtime.memory.retrieve(""))
                return super().build(runtime)
        self.bench.run(Scenario.from_dict(ticket()), Write(), self.persona)
        self.assertEqual([r.text for r in captured], ["Исправленное значение"])

    def test_archive_is_excluded_even_if_it_appears_in_change_history(self):
        from types import SimpleNamespace
        from iam.experiments.memory import BenchMemory
        archive = MemoryRecord("mem_Archive001", "exp_Example001", "archive", "semantic", "simulation_event",
                               {"text": "АРХИВ"}, Source("core", "fixture"), "2026-01-01T00:00:00.000Z", 0,
                               ("evt_Example001",))
        fake = SimpleNamespace(events_after=lambda seq: [SimpleNamespace(payload={"changes": [
            {"group": "memory", "path": archive.memory_id}]} )],
            store=SimpleNamespace(get_memory=lambda mid: archive))
        self.assertEqual(BenchMemory(fake).retrieve(""), [])

    def test_same_invalid_response_has_deterministic_error_trace(self):
        class Invalid:
            name = "invalid_fixture"
            def build(self, runtime):
                from iam.stubs import rest
                def bad(request):
                    p = rest(cites=[request["context"][0]["ref"]])
                    p["body"] = {"invalid": True}
                    return Reply(proposals=(p,))
                return runtime.cycle(ScriptedProvider([bad, bad]))
        a = self.bench.run(Scenario.from_dict(ticket()), Invalid(), self.persona)[0]
        b = self.bench.run(Scenario.from_dict(ticket()), Invalid(), self.persona)[0]
        self.assertEqual(a.trace_bytes, b.trace_bytes)
        self.assertEqual(a.trace["decision_trace"][0]["report"]["outcome"], "rejected")
        self.assertTrue(any(e["payload"].get("fault") == "bad_response" for e in a.trace["journal"]))

    def test_no_training_frameworks_in_sources_or_dependencies(self):
        import ast
        import tomllib
        forbidden = {"torch", "tensorflow", "jax", "keras", "transformers"}
        for path in (ROOT / "src").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = ([node.module] if isinstance(node, ast.ImportFrom) and node.module else
                           [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                self.assertFalse({m.split(".")[0] for m in modules} & forbidden, str(path))
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        deps = project["dependencies"] + sum(project.get("optional-dependencies", {}).values(), [])
        self.assertFalse(any(d.split("=")[0] in forbidden for d in deps))

    def test_empty_no_decisions_stop_and_duplicates(self):
        sc = ticket(0)
        sc["step"].extend([{"id": "a", "kind": "input", "sender": "operator", "text": "доставка", "deliveries": 2},
                            {"id": "stop", "kind": "session", "action": "stop"}])
        (result,) = self.run_ticket(sc)
        self.assertEqual(result.trace["decisions"], 0)
        self.assertEqual(sum(e["type"] == "input.arrived" for e in result.trace["journal"]), 1)

    def test_cli(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            rc = main(["--data-root", str(self.root), "--scenario", str(CATALOG / "exp_no_tool.toml"),
                       "--persona", str(PROFILES / "social.toml"), "--seed", "7", "--motivation-off"])
        self.assertEqual(rc, 0)
        self.assertIn("trace.json", output.getvalue())
        self.assertIn("toy,7,False,True", output.getvalue())


class LoadingCase(unittest.TestCase):
    setUp = BenchCase.setUp
    run_ticket = BenchCase.run_ticket
    def test_loader_validation_rejects_without_output_files(self):
        cases = []
        for field, value in (("format", 2), ("group", "other"), ("id", "../../escape")):
            bad = ticket()
            bad[field] = value
            cases.append(bad)
        cases.extend([
            ticket(run=[{"id": "one"}]),
            ticket(run=[{"id": "one"}, {"id": "one"}]),
            ticket(run=[{"id": "one", "switches": {"unknown": "off"}}, {"id": "two"}]),
            ticket(step=[]),
        ])
        bad_steps = [
            {"id": "start", "kind": "session", "action": "start"},
            {"id": "x", "kind": "input", "run": "missing"},
            {"id": "x", "kind": "operator"},
            {"id": "x", "kind": "session", "action": "crash"},
            {"id": "x", "kind": "model", "delivery": "stale"},
            {"id": "x", "kind": "world", "outcome": "magic"},
            {"id": "x", "kind": "wait", "life_ms": True},
            {"id": "x", "kind": "wait", "calendar_ms": 0},
            {"id": "x", "kind": "wait", "calendar_ms": 1, "life_ms": 1},
        ]
        for step in bad_steps:
            bad = ticket()
            bad["step"].append(step)
            cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.run_ticket(bad)
        self.assertFalse(self.bench.results.root.exists())

    def test_wrong_seed_persona_or_missing_start_is_rejected_before_files(self):
        for data, options in ((ticket(seed=True), {}), (ticket(persona="other"), {}),
                              (ticket(step=[{"id": "choose", "kind": "decide", "situation": "x"}]), {})):
            with self.assertRaises(ValueError):
                self.run_ticket(data, **options)
        self.assertFalse(self.bench.results.root.exists())

    def test_persona_validation(self):
        self.assertEqual(len(list(PROFILES.glob("*.toml"))), 3)
        samples = [
            'format = 1\nid = "x"\ntemporary = false\n',
            'format = 1\nid = "x"\ntemporary = true\n',
            'format = 1\nid = "x"\ntemporary = true\nname = "x"\nmotives = [""]\n[traits]\n',
        ]
        for i, sample in enumerate(samples):
            path = self.root / f"bad{i}.toml"
            path.write_text(sample)
            with self.assertRaises(ValueError):
                Persona.load(path)

    def test_normalizer_preserves_references_and_ordinary_values(self):
        normalize = Normalizer()
        result = normalize({"event_id": "evt_Example001", "causes": ["evt_Example001"], "n": 42,
                            "nested": {"evt_Example001": "ordinary"}, "permanent": "per_Permanent01"})
        self.assertEqual(result["event_id"], result["causes"][0])
        self.assertIn(result["event_id"], result["nested"])
        self.assertEqual(result["n"], 42)
        self.assertEqual(result["permanent"], "per_Permanent01")

    def test_normalizer_keeps_user_literals_and_actual_id_mentions_in_prose(self):
        normalize = Normalizer()
        raw = {"event_id": "evt_Example001", "text": "evt_Example001 evt_AAAAAAAA",
               "params": {"code": "evt_Example001"}, "content": {"text": "mem_BBBBBBBB"}}
        result = normalize(raw)
        for key in ("text", "params", "content"):
            self.assertEqual(result[key], raw[key])
        self.assertNotEqual(result["event_id"], raw["event_id"])
        self.assertNotEqual(Normalizer()({"text": "evt_AAAAAAAA"}), Normalizer()({"text": "evt_BBBBBBBB"}))

    def test_normalizer_does_not_hide_different_user_text_in_trace(self):
        a = ticket()
        a["step"].insert(1, {"id": "input", "kind": "input", "sender": "operator", "text": "evt_AAAAAAAA"})
        b = copy.deepcopy(a)
        b["step"][1]["text"] = "evt_BBBBBBBB"
        self.assertNotEqual(self.run_ticket(a)[0].trace_bytes, self.run_ticket(b)[0].trace_bytes)

    def test_runtime_require_fails_on_rejected_fixture(self):
        from iam.experiments.runtime import Runtime
        from iam.state import ApplyResult
        with self.assertRaisesRegex(ValueError, "ядро отклонило"):
            Runtime.require(ApplyResult("rejected", 1, 0, reason="fixture_error"))
