"""Проверка границ живого запуска без внешней модели."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from iam.providers.run import RunResult
from iam.stubs import Reply, ScriptedProvider, rest

from integration import live_series as series


class SeriesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="iam-s25-series-test-")
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "series"

    def test_fifty_four_choices_and_twelve_control_branches_preserve_common_past(self):
        result = series.run_series(self.output)
        self.assertEqual((result["status"], result["decisions"], result["model_calls"]),
                         ("completed", 54, 0))
        self.assertIsNone(result["behavioral_success"])
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(len(manifest["initial"]), 5)
        for initial in manifest["initial"].values():
            self.assertEqual(series.sha(self.output / initial["db"]), initial["sha256"])
            self.assertEqual(series.state_digest(self.output / initial["db"]), initial["history_digest"])
        sessions = result["sessions"]
        self.assertEqual(len(sessions), 27)
        for story, checkpoint, switch in series.PAIRS:
            frozen = json.loads((self.output / f"checkpoints/{story}-{checkpoint}.json").read_text())
            self.assertEqual(series.sha(self.output / f"checkpoints/{story}-{checkpoint}.db"), frozen["sha256"])
            matches = [session for session in sessions if session["name"].startswith(f"{story}-{switch}-")]
            self.assertEqual(len(matches), 4)
            self.assertEqual({session["before_digest"] for session in matches}, {frozen["history_digest"]})
        request = json.loads((self.output / "sessions/A-1/requests/001.json").read_text())["request"]
        self.assertIn("экспериментальная история", request["context"][-1]["text"])
        with self.assertRaises(FileExistsError):
            series.run_series(self.output)

    def test_global_decision_and_forecast_caps_stop_before_an_extra_call(self):
        with patch.object(series, "MAX_DECISIONS", 1):
            result = series.run_series(self.output)
        self.assertEqual((result["status"], result["decisions"], result["model_calls"]), ("stopped", 1, 0))
        self.assertEqual(result["diagnostic"], "global_decisions")

    def test_every_physical_attempt_is_reserved_and_four_per_decision_is_the_limit(self):
        made = []

        def factory(name, transport):
            script = ScriptedProvider()
            class UsesTransport:
                name = "test_transport"
                def propose(self, request):
                    made.append(request["request_id"])
                    for _ in range(5):
                        transport(("docker", "test"), "NEVER_PERSIST_STDIN", 1)
                    script.queue(Reply((rest(),)))
                    return script.propose(request)
            return UsesTransport()

        with patch.object(series, "subprocess_runner", return_value=RunResult(0)) as transport:
            result = series.run_series(self.output, mode="live",
                                       auth_file=str(self.output.parent / "external-private.json"),
                                       provider_factory=factory)
        self.assertEqual((result["status"], result["decisions"], result["model_calls"]), ("stopped", 1, 4))
        self.assertEqual(transport.call_count, 4)
        self.assertEqual(len(made), 1)
        self.assertTrue(all(attempt["status"] == "returned" for attempt in result["attempts"]))
        for path in self.output.rglob("*.json"):
            self.assertNotIn("NEVER_PERSIST_STDIN", path.read_text())
            self.assertNotIn("external-private.json", path.read_text())

    def test_changed_sources_stop_before_the_first_model_choice(self):
        initial = series.sources()
        with patch.object(series, "sources", side_effect=[initial, initial, {**initial, "changed": "yes"}]):
            result = series.run_series(self.output)
        self.assertEqual((result["status"], result["decisions"], result["model_calls"]), ("stopped", 0, 0))
        self.assertEqual(result["diagnostic"], "frozen_sources_changed")

    def test_empty_choice_stops_and_cannot_become_a_completed_history(self):
        def factory(name, transport):
            return ScriptedProvider([Reply(())])
        result = series.run_series(self.output, provider_factory=factory)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["decisions"], 1)
        self.assertEqual(result["diagnostic"], "provider_or_empty_choice")
        self.assertIsNone(result["behavioral_success"])

    def test_hidden_memory_reference_is_stopped_before_transport(self):
        original = series.ExperimentCycle._context
        def leak(cycle, trigger, focus, records):
            context = original(cycle, trigger, focus, records)
            context.append({"ref": "mem_HiddenPast01", "origin": "observation", "layer": "personal",
                            "text": "Недоступное прошлое"})
            return context
        def factory(name, transport):
            class UsesTransport:
                name = "test_transport"
                def propose(self, request):
                    transport(("docker", "test"), "PRIVATE", 1)
                    return ScriptedProvider([Reply((rest(),))]).propose(request)
            return UsesTransport()
        with patch.object(series.ExperimentCycle, "_context", leak), \
             patch.object(series, "subprocess_runner", return_value=RunResult(0)) as transport:
            result = series.run_series(self.output, mode="live",
                       auth_file=str(self.output.parent / "private.json"), provider_factory=factory)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["model_calls"], 0)
        transport.assert_not_called()
        trace = json.loads((self.output / "sessions/A-1/requests/001.json").read_text())
        self.assertEqual(trace["error"]["diagnostic"], "hidden_memory_reference")

    def test_bad_semantic_appraisal_stops_after_valid_free_choice(self):
        from iam.domain import new_id
        from iam.stubs import act
        from iam.world import READ, WRITE
        def factory(name, transport):
            script = ScriptedProvider()
            class ProposesThenBadAppraisal:
                name = "scripted"
                def __init__(self):
                    self.choices = 0
                def propose(self, request):
                    if request["purpose"] == "appraise":
                        answer = rest()  # envelope valid, rubric fields absent
                    elif name == "A-1":
                        self.choices += 1
                        catalog = next(json.loads(item["text"]) for item in request["context"]
                                       if item["text"].startswith("{") and '"library"' in item["text"])
                        if self.choices == 1:
                            answer = act(READ, {"material_id": next(iter(catalog["library"]))})
                        else:
                            ref = next(item["ref"] for item in request["context"]
                                       if item["ref"].startswith("mem_") and item["origin"] == "observation")
                            goal = {"proposal_id": new_id("prop"), "kind": "intention", "rationale": "Развить опыт",
                                    "cites": [ref], "body": {"op": "create", "intention_id": "light",
                                      "text": "Написать строку о свете", "criterion": "Слово свет",
                                      "project_id": "light", "operation": "write"}}
                            script.queue(Reply((goal, act(WRITE, {"project_id": "light", "title": "Свет", "text": "Свет."}))))
                            return script.propose(request)
                    elif name == "A-2":
                        catalog = next(json.loads(item["text"]) for item in request["context"]
                                       if item["text"].startswith("{") and '"world_results"' in item["text"])
                        result = next(row for row in catalog["world_results"] if row["target"] == "light")
                        answer = {"proposal_id": new_id("prop"), "kind": "intention", "rationale": "Проверить",
                                  "cites": [result["memory_id"], result["event_id"]],
                                  "body": {"op": "transition", "intention_id": "light", "to_status": "done",
                                           "result_ref": result["memory_id"]}}
                    else:
                        answer = rest()
                    script.queue(Reply((answer,)))
                    return script.propose(request)
            return ProposesThenBadAppraisal()
        result = series.run_series(self.output, provider_factory=factory)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["diagnostic"], "recorded_technical_error")
        self.assertEqual(result["decisions"], 3)
        entry = json.loads((self.output / "sessions/A-2/decisions/01.json").read_text())
        self.assertEqual(entry["assessment_errors"][0]["error"], "assessment_format_or_cites")

    def test_failed_memory_stage_stops_after_confirmed_world_action(self):
        from iam.memory import PersistentMemory
        from iam.stubs import act
        from iam.world import READ
        def read(request):
            catalog = next(json.loads(item["text"]) for item in request["context"]
                           if item["text"].startswith("{") and '"library"' in item["text"])
            return Reply((act(READ, {"material_id": next(iter(catalog["library"]))}),))
        def factory(name, transport):
            return ScriptedProvider([read])
        with patch.object(PersistentMemory, "write", return_value=SimpleNamespace(applied=False, events=())):
            result = series.run_series(self.output, provider_factory=factory)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["diagnostic"], "recorded_technical_error")
        self.assertEqual(result["decisions"], 1)
        entry = json.loads((self.output / "sessions/A-1/decisions/01.json").read_text())
        self.assertTrue(any(fault["source"] == "stage" and fault["stage"]["stage"] == "memory_update"
                            for fault in entry["technical_faults"]))

    def test_changed_sources_during_the_last_response_prevent_completed_status(self):
        changed = {"value": False}
        original_sources = series.sources

        def current_sources():
            result = original_sources()
            return {**result, "changed_at_last_response": "yes"} if changed["value"] else result

        def factory(name, transport):
            script = ScriptedProvider()
            class LastChange:
                name = "scripted"
                def __init__(self):
                    self.calls = 0
                def propose(self, request):
                    self.calls += 1
                    if name == "C-memory-2-memory" and self.calls == 2:
                        changed["value"] = True
                    script.queue(Reply((rest(),)))
                    return script.propose(request)
            return LastChange()

        with patch.object(series, "sources", current_sources):
            result = series.run_series(self.output, provider_factory=factory)
        self.assertEqual((result["status"], result["decisions"]), ("stopped", 54))
        self.assertEqual(result["diagnostic"], "frozen_sources_changed")

    def test_hidden_text_without_a_memory_reference_is_rejected(self):
        from iam.domain import new_id
        from iam.state import StateController
        from iam.storage import Store
        from integration.test_persistent_core import wire

        with Store.create(self.output.parent / "history.db", new_id("exp")) as store:
            controller = StateController(store)
            controller.create_persona()
            session, cycle, _ = wire(controller)
            session.command("start", "Опыт для проверки утечки")
            source = cycle.memory.prepare(kind="semantic", origin="own_hypothesis",
                         text="Недоступный уникальный опыт", sources=(store.get_event(1).event_id,))
            self.assertTrue(cycle.memory.write(source).applied)
            self.assertTrue(cycle.memory.fade(source.memory_id).applied)
            request = {"context": [{"ref": store.get_event(1).event_id, "origin": "model_knowledge",
                                     "layer": "state", "text": source.text}]}
            with self.assertRaisesRegex(series.StopSeries, "hidden_memory_content"):
                series.check_request(request, cycle.memory)

    def test_initial_curious_profile_has_no_prepared_experience_and_off_hides_motives(self):
        from iam.state import StateController
        from iam.storage import Store
        from integration.test_persistent_core import choose, wire
        for off in (False, True):
            path = self.output.parent / ("motives-off.db" if off else "curious.db")
            series.initial_database(path, "B")
            with Store.open(path) as store:
                ctl = StateController(store)
                self.assertEqual(ctl.state["intentions"], {})
                self.assertEqual(ctl.readable_memories(), [])
                self.assertEqual(ctl.state["traits"]["traits"], [
                    {"name": key, "value": value, "source": "initial"} for key, value in series.PROFILE["traits"].items()])
                session, cycle, provider = wire(ctl, cycle_type=series.ExperimentCycle, motivation_off=off)
                self.assertEqual(cycle.world.state.get("works", {}), {})
                session.command("start", "Только начальный характер")
                self.assertEqual(choose(cycle, provider, rest()).outcome, "done")
                context = provider.calls[-1]["context"]
                profile = series.profile_context(cycle.memory)
                self.assertIn(profile, context)
                entry = json.loads(profile["text"])["initial_profile"]
                self.assertEqual(entry["motives"], [] if off else series.PROFILE["motives"])
                if off:
                    for motive in series.PROFILE["motives"]:
                        self.assertNotIn(motive, json.dumps(context, ensure_ascii=False))
                series.check_request(provider.calls[-1], cycle.memory)

    def test_only_exact_initial_profile_can_overlap_hidden_text(self):
        from iam.state import StateController
        from iam.storage import Store
        from integration.test_persistent_core import wire
        path = self.output.parent / "profile-collision.db"
        series.initial_database(path, "B")
        with Store.open(path) as store:
            ctl = StateController(store)
            session, cycle, _ = wire(ctl, cycle_type=series.ExperimentCycle)
            session.command("start", "Проверка совпадающих фраз")
            source = cycle.memory.prepare(kind="semantic", origin="own_hypothesis", text=series.PROFILE["motives"][0],
                                          sources=(store.get_event(1).event_id,))
            self.assertTrue(cycle.memory.write(source).applied)
            cycle.memory.off = True
            trusted = series.profile_context(cycle.memory)
            series.check_request({"context": [trusted]}, cycle.memory)
            forged = {**trusted, "text": trusted["text"] + " Личный опыт: " + source.text}
            with self.assertRaisesRegex(series.StopSeries, "hidden_memory_content"):
                series.check_request({"context": [forged]}, cycle.memory)
            with self.assertRaisesRegex(series.StopSeries, "hidden_memory_reference"):
                series.check_request({"context": [{**trusted, "ref": source.memory_id}]}, cycle.memory)

    def test_motivation_off_rejects_initial_motive_cached_as_plain_semantic_memory(self):
        from iam.state import StateController
        from iam.storage import Store
        from integration.test_persistent_core import choose, wire
        path = self.output.parent / "cached-motive.db"
        series.initial_database(path, "B")
        with Store.open(path) as store:
            ctl = StateController(store)
            session, cycle, provider = wire(ctl, cycle_type=series.ExperimentCycle)
            session.command("start", "Кеш начального мотива")
            note = {"proposal_id": series.new_id("prop"), "kind": "memory_write", "rationale": "Свой мотив",
                    "cites": [store.get_event(1).event_id], "body": {"draft": {"kind": "semantic",
                    "origin": "own_hypothesis", "content": {"text": series.PROFILE["motives"][1]}}}}
            self.assertEqual(choose(cycle, provider, note).outcome, "done")
            cached, = cycle.memory.records()
            self.assertNotIn("motivation", cached.content)
            cycle.memory.motivation_off = True
            self.assertIn(cached.memory_id, [r.memory_id for r in cycle.memory.records()])
            with self.assertRaisesRegex(series.StopSeries, "hidden_memory_content"):
                series.check_request({"context": [{"ref": cached.memory_id, "origin": cached.origin,
                                                    "layer": cached.layer, "text": cached.text}]}, cycle.memory)
