"""S22 runner checks: neutral full plan, persistent fixtures, bounded transport, sanitized failures."""
import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from iam.backup.scan import SecretScanner
from iam.providers.errors import ProviderFormatError, ProviderUnavailable
from iam.providers.run import RunResult

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/world/series.py"
spec = importlib.util.spec_from_file_location("s22_series", SCRIPT)
series = importlib.util.module_from_spec(spec)
spec.loader.exec_module(series)


class SeriesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="iam-s22-series-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def read(self, relative, root=None):
        return json.loads(((root or self.root / "run") / relative).read_text())

    def appraisal_factory(self, verdict, target="interest_emergence-main-1"):
        def factory(cell, transport):
            class CreatesWritesThenCompletes:
                name = "scripted"

                def __init__(self):
                    self.choices = 0
                    self.script = series.ScriptedProvider()

                def propose(self, request):
                    if request["purpose"] == "appraise":
                        packet = json.loads(request["context"][0]["text"])["packet"]
                        if verdict == "malformed":
                            proposals = (series.rest(),)  # valid envelope, wrong appraisal fields/cites
                        else:
                            if "available_result" not in packet:
                                body = dict(required_kind="uncertain" if verdict == "requirement_uncertain" else "artifact",
                                            support_quote=packet["goal"]["text"], reason="Отдельная проверка вида результата")
                            else:
                                body = dict(verdict=verdict, goal_met=False, criterion_met=False, evidence_sufficient=False,
                                            support_quote=packet["available_result"]["text"], reason="Критерий не подтверждён")
                            proposals = (dict(proposal_id=series.new_id("prop"), kind="no_action", rationale="Отдельная оценка",
                                              cites=[request["context"][0]["ref"]], body=body),)
                    else:
                        self.choices += 1
                        if cell["cell"] != target:
                            proposals = (series.rest(),)
                        elif self.choices == 1:
                            source = next(item["ref"] for item in request["context"]
                                          if item["ref"].startswith("mem_") and item["origin"] == "observation")
                            goal = dict(proposal_id=series.new_id("prop"), kind="intention", rationale="Развить прочитанную тему",
                                        cites=[source], body=dict(op="create", intention_id="assessed_notes", text="Написать абзац о свете",
                                                                 criterion="Есть описание вечернего света", project_id="assessed_project", operation="write"))
                            proposals = (goal, series.act(series.WRITE, dict(project_id="assessed_project", title="Вечер", text="Вечерний свет ложится на стол.")))
                        elif self.choices == 2:
                            catalog = next(json.loads(item["text"]) for item in request["context"]
                                           if item["text"].startswith("{") and '"world_results"' in item["text"])
                            result = next(row for row in catalog["world_results"] if row["target"] == "assessed_project")
                            proposals = (dict(proposal_id=series.new_id("prop"), kind="intention", rationale="Проверить результат",
                                              cites=[result["memory_id"], result["event_id"]], body=dict(op="transition", intention_id="assessed_notes",
                                                                                                      to_status="done", result_ref=result["memory_id"])),)
                        else:
                            proposals = (series.rest(),)
                    self.script.queue(series.Reply(proposals))
                    return self.script.propose(request)
            return CreatesWritesThenCompletes()
        return factory

    def synthetic_original(self, output):
        factory = self.appraisal_factory("malformed", target="return_unfinished-main-1")

        def counted_factory(cell, transport):
            inner = factory(cell, transport)

            class Counted:
                name = inner.name

                def propose(self, request):
                    transport(("docker", "fake-model"), "never-auth", 5, ("docker", "kill", "fake"))
                    return inner.propose(request)
            return Counted()

        with patch.object(series, "subprocess_runner", return_value=RunResult(0)) as run, \
                patch.object(series, "VERSION", "s22-series-1"), patch.object(series, "NATIVE_FORMAT_RETRIES", 0):
            result = series.run_series(output, mode="live", auth_file="/external/not-read", provider_factory=counted_factory)
        self.assertEqual((result["status"], result["stop"], result["decisions_started"], result["actual_model_calls"]),
                         ("partial", "assessment_error", 18, 19), result)
        self.assertEqual(run.call_count, 19)
        return result

    def synthetic_second(self, original, output):
        self.synthetic_original(original)

        def factory(cell, transport):
            scripted = series.ScriptedProvider()

            class Legacy:
                name = "scripted"

                def __init__(self):
                    self.requests, self.last_calls = 0, 0

                def propose(self, request):
                    self.last_calls = 0
                    self.requests += 1
                    transport(("docker", "fake-model"), "not-auth", 5, ("docker", "kill", "fake"))
                    self.last_calls += 1
                    if cell["cell"] == "return_unfinished-main-2" and self.requests == 2:
                        raise ProviderFormatError("Codex response did not match the required format")
                    scripted.queue(series.Reply((series.rest(),)))
                    return scripted.propose(request)
            return Legacy()

        with patch.object(series, "subprocess_runner", return_value=RunResult(0)), \
                patch.object(series, "VERSION", "s22-series-2"), patch.object(series, "NATIVE_FORMAT_RETRIES", 0):
            result = series.run_series(output, mode="live", auth_file="/external/not-read", repair_from=original, provider_factory=factory)
        self.assertEqual((result["status"], result["stop"], result["decisions_started"], result["actual_model_calls"]),
                         ("partial", "provider_or_cycle_failure", 22, 23), result)
        return result

    def neutral_factory(self, *, constructed=None, requested=None, fail_cell=None, fail_at=2,
                        codes=("final_json",), error_type=ProviderFormatError):
        def factory(cell, transport):
            if constructed is not None:
                constructed.append(cell["cell"])
            scripted = series.ScriptedProvider()

            class Neutral:
                name = "scripted"

                def __init__(self):
                    self.count, self.last_calls, self.last_format_codes = 0, 0, []

                def propose(self, request):
                    self.last_calls, self.last_format_codes = 0, []
                    self.count += 1
                    if requested is not None:
                        requested.append(cell["cell"])
                    transport(("docker", "fake-model"), "not-auth", 5, ("docker", "kill", "fake"))
                    self.last_calls += 1
                    if cell["cell"] == fail_cell and self.count == fail_at:
                        self.last_format_codes = list(codes)
                        raise error_type("Safe fixed format failure")
                    scripted.queue(series.Reply((series.rest(),)))
                    return scripted.propose(request)
            return Neutral()
        return factory

    @staticmethod
    def snapshot_files(root):
        return {str(path.relative_to(root)): series.sha(path) for path in root.rglob("*") if path.is_file()}

    def test_plan_is_18_cells_and_60_decisions(self):
        cells = series.plan()
        self.assertEqual(len(cells), 18)
        self.assertEqual(len({cell["cell"] for cell in cells}), 18)
        self.assertEqual(sum(c["max_decisions"] for c in cells), 60)
        self.assertEqual(sum(c["max_decisions"] for c in cells if not c["motivation_off"]), 48)
        self.assertEqual(sum(c["max_decisions"] for c in cells if c["motivation_off"]), 12)
        for family in series.FAMILIES:
            branch = [c for c in cells if c["family"] == family]
            self.assertEqual({c["repeat"] for c in branch}, {1, 2})

    def test_appraisal_has_one_reference_and_neutral_experiment_metadata(self):
        scripted = series.ScriptedProvider()
        audit = series.AuditedProvider(scripted, series.Artifacts(self.root), "test")
        packet = dict(goal=dict(text="Написать заметку", criterion="Есть абзац"))
        envelope = dict(instructions="Определи необходимый вид свидетельства.", packet=packet)
        query = dict(request_id="req_ReferenceTest01", epoch=1, state_version=1, purpose="appraise",
                     context=[dict(ref="evt_OnlyReference01", origin="observation", layer="state", text=json.dumps(envelope))])
        scripted.queue(series.Reply((series.rest(),)))
        audit.propose(query)
        actual = scripted.calls[0]
        self.assertEqual(len(actual["context"]), 1)
        self.assertEqual(actual["context"][0]["ref"], "evt_OnlyReference01")
        payload = json.loads(actual["context"][0]["text"])
        self.assertEqual(payload["instructions"], envelope["instructions"])
        self.assertEqual(payload["packet"], packet)
        self.assertEqual(payload["experiment_metadata"], dict(scripted_past=True, prior_choices_not_autonomous=True))
        self.assertNotIn(series.DISCLOSURE, json.dumps(actual, ensure_ascii=False))
        free = dict(query, purpose="choose_action", request_id="req_ReferenceTest02")
        scripted.queue(series.Reply((series.rest(),)))
        audit.propose(free)
        expected = [*free["context"], dict(ref=free["context"][0]["ref"], origin="observation", layer="state", text=series.DISCLOSURE)]
        self.assertEqual(scripted.calls[1]["context"], expected)
        self.assertEqual(json.loads(query["context"][0]["text"]), envelope)

    def test_full_stub_is_neutral_and_uses_identical_baselines(self):
        with patch.object(series, "subprocess_runner", side_effect=AssertionError("No live traffic")) as run:
            result = series.run_series(self.root / "run")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual((result["decisions_started"], result["actual_model_calls"], result["typed_requests"]), (60, 0, 60))
        run.assert_not_called()
        manifest = self.read("manifest.json")
        self.assertTrue(manifest["scripted_past"])
        self.assertEqual(manifest["model"], "gpt-6-luna")
        self.assertEqual(manifest["budgets"]["provider_format_retries"], 1)
        self.assertEqual(manifest["budgets"]["cycle_stale_retries"], 0)
        self.assertEqual(result["manifest_sha256"], series.sha(self.root / "run/manifest.json"))
        for cell in result["cells"]:
            self.assertEqual(cell["baseline_sha256"], manifest["baselines"][cell["family"]]["sha256"])
            self.assertEqual(len(cell["decisions"]), cell["max_decisions"])
            self.assertIsNone(cell["behavioral_success"])
            trace = self.read(f"cells/{cell['cell']}/requests/001.json")
            self.assertIn(series.DISCLOSURE, [x["text"] for x in trace["request"]["context"]])
            self.assertEqual([p["kind"] for p in trace["response"]["proposals"]], ["no_action"])
            self.assertNotIn(cell["scenario_id"], json.dumps(trace["request"]))
            if cell["motivation_off"]:
                catalogs = []
                for item in trace["request"]["context"]:
                    try:
                        catalogs.append(json.loads(item["text"]))
                    except ValueError:
                        pass
                world = next(c for c in catalogs if isinstance(c, dict) and "world_results" in c)
                self.assertEqual(world["intentions"], {})
                self.assertEqual(world["motivation"], {"enabled": False})
        fixture = self.read("baselines/return_unfinished/fixture.json")
        self.assertEqual(fixture["state"]["intentions"]["window_notes"]["status"], "deferred")
        repetition = self.read("baselines/interest_loss/fixture.json")
        works = repetition["state"]["world_time"]["world"]["works"]
        self.assertEqual(len(works), 3)
        self.assertEqual(len({w["fingerprint"] for w in works.values()}), 1)
        failure = self.read("baselines/failure_reaction/fixture.json")
        self.assertEqual(sum(e["type"] == "action.failed" for e in failure["events"]), 2)
        self.assertEqual(failure["state"]["world_time"]["world"].get("works", {}), {})
        for artifact in (self.root / "run").rglob("*.json"):
            self.assertFalse(SecretScanner(use_env=True).find(artifact.read_text()), artifact)

    def test_partial_transport_failure_is_charged_and_redacted(self):
        secret = "s22CanaryCredential987654321"

        def factory(cell, transport):
            class Broken:
                name = "codex"

                def propose(self, request):
                    transport(("docker", "fake-model"), "never-record-stdin-" + secret, 5, ("docker", "kill", "fake"))
                    raise ProviderUnavailable("Unavailable " + secret)
            return Broken()

        scanner = SecretScanner(canaries=[secret], use_env=False)
        with patch.object(series, "subprocess_runner", return_value=RunResult(127, "", "container missing")) as run:
            result = series.run_series(self.root / "run", mode="live", auth_file="/external/not-read", provider_factory=factory, scanner=scanner)
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual((result["actual_model_calls"], result["decisions_started"]), (1, 1))
        self.assertEqual(len(result["cells"]), 1)
        self.assertEqual(run.call_args.args[3], ("docker", "kill", "fake"))
        trace = self.read("cells/interest_emergence-main-1/requests/001.json")
        self.assertEqual(trace["error"]["error_class"], "ProviderUnavailable")
        self.assertNotIn(secret, json.dumps(trace))
        saved = self.read("summary.json")
        self.assertNotIn(secret, json.dumps(saved))
        self.assertEqual(self.read("calls.json")["actual_model_calls"], 1)
        self.assertTrue((self.root / "run/cells/interest_emergence-main-1/decisions/01.json").exists())

    def test_new_intentions_only_counts_creation_in_current_decision(self):
        def factory(cell, transport):
            class CreatesOnce:
                name = "scripted"

                def __init__(self):
                    self.first = True
                    self.script = series.ScriptedProvider()

                def propose(self, request):
                    if self.first and cell["family"] == "interest_emergence":
                        self.first = False
                        source = next(item["ref"] for item in request["context"]
                                      if item["ref"].startswith("mem_") and item["origin"] == "observation")
                        proposal = dict(proposal_id=series.new_id("prop"), kind="intention", rationale="Продолжить прочитанную тему",
                                        cites=[source], body=dict(op="create", intention_id="fresh_notes", text="Написать заметку о вечернем свете",
                                                                 criterion="Есть абзац о свете", project_id="new_light_notes", operation="write"))
                    else:
                        proposal = series.rest()
                    self.script.queue(series.Reply((proposal,)))
                    return self.script.propose(request)
            return CreatesOnce()

        with patch.object(series, "subprocess_runner", side_effect=AssertionError("No live traffic")):
            result = series.run_series(self.root / "run", provider_factory=factory)
        self.assertEqual(result["status"], "completed", result)
        for cell in result["cells"]:
            if cell["family"] == "interest_emergence":
                self.assertEqual(cell["decisions"][0]["new_intentions"], ["fresh_notes"])
                self.assertEqual(cell["decisions"][1]["new_intentions"], [])
                state = self.read(f"cells/{cell['cell']}/decisions/02.json")["state"]
                self.assertIn("fresh_notes", state["intentions"])

    def test_auth_paths_are_redacted_from_configuration_and_transport_errors(self):
        credentials = self.root / "private-IAM-managed.json"
        secret = "keepCustomCanaryScrubbing987654321"
        scanner = SecretScanner(canaries=[secret], use_env=False)

        def configuration_failure(cell, transport):
            raise FileNotFoundError(2, "Configuration " + secret, str(credentials))

        def transport_failure(cell, transport):
            class Broken:
                name = "codex"

                def propose(self, request):
                    transport(("docker", "fake-model"), "never-read-or-saved", 5, ("docker", "kill", "fake"))
            return Broken()

        for label, factory in (("configuration", configuration_failure), ("transport", transport_failure)):
            with self.subTest(label=label):
                output = self.root / label
                with patch.object(series, "subprocess_runner", side_effect=FileNotFoundError(2, "Transport " + secret, str(credentials))):
                    result = series.run_series(output, mode="live", auth_file=credentials, provider_factory=factory, scanner=scanner)
                self.assertEqual(result["status"], "partial", result)
                self.assertNotIn(str(credentials), json.dumps(result))
                self.assertNotIn(secret, json.dumps(result))
                for artifact in output.rglob("*.json"):
                    content = artifact.read_text()
                    self.assertNotIn(str(credentials), content, artifact)
                    self.assertNotIn(secret, content, artifact)
                for db in output.rglob("*.db"):
                    with series.Store.open(db, readonly=True) as store:
                        journal = json.dumps([e.to_dict() for e in store.events()])
                    self.assertNotIn(str(credentials), journal, db)
                    self.assertNotIn(secret, journal, db)
                if label == "transport":
                    trace = self.read("cells/interest_emergence-main-1/requests/001.json", output)
                    self.assertEqual(trace["error"]["error_class"], "FileNotFoundError")
                else:
                    self.assertEqual(result["error"]["error_class"], "FileNotFoundError")
        self.assertEqual(scanner.known, (secret,))

    def test_malformed_appraisal_stops_whole_series_without_repeat(self):
        from iam.intentions.completion import CompletionProvider
        original_record = CompletionProvider.record

        def pause_before_record(owner, rid, value):
            if value.get("phase") == "requirement" and value.get("error"):
                owner.intentions.ctl.pause_session()
            return original_record(owner, rid, value)

        for paused in (False, True):
            with self.subTest(paused=paused):
                output = self.root / ("paused" if paused else "running")
                pause = patch.object(CompletionProvider, "record", pause_before_record) if paused else nullcontext()
                with pause, patch.object(series, "subprocess_runner", side_effect=AssertionError("No live traffic")) as run:
                    result = series.run_series(output, provider_factory=self.appraisal_factory("malformed"))
                run.assert_not_called()
                self.assertEqual((result["status"], result["stop"]), ("partial", "assessment_error"), result)
                self.assertEqual((result["decisions_started"], result["typed_requests"], len(result["cells"])), (2, 3, 1))
                self.assertEqual(result["cells"][0]["error"]["error_class"], "AssessmentError")
                decision = self.read("cells/interest_emergence-main-1/decisions/02.json", output)
                self.assertIsNone(decision["provider_error"])
                self.assertEqual(decision["assessment_errors"][0]["error"], "assessment_format_or_cites")
                self.assertEqual(decision["assessment_errors"][0]["event_type"], "system.error" if paused else "proposal.assessed")
                self.assertEqual(decision["state"]["intentions"]["assessed_notes"]["status"], "active")
                trace = self.read("cells/interest_emergence-main-1/requests/003.json", output)
                self.assertEqual(trace["request"]["purpose"], "appraise")
                self.assertEqual(trace["response"]["proposals"][0]["cites"], [])
                for artifact in output.rglob("*.json"):
                    self.assertFalse(SecretScanner(use_env=True).find(artifact.read_text()), artifact)

    def test_valid_negative_or_uncertain_appraisal_does_not_stop_series(self):
        for verdict in ("fail", "uncertain", "requirement_uncertain"):
            with self.subTest(verdict=verdict):
                output = self.root / verdict
                with patch.object(series, "subprocess_runner", side_effect=AssertionError("No live traffic")):
                    result = series.run_series(output, provider_factory=self.appraisal_factory(verdict))
                self.assertEqual((result["status"], result["stop"], result["decisions_started"]), ("completed", None, 60), result)
                self.assertEqual(len(result["cells"]), 18)
                decision = self.read("cells/interest_emergence-main-1/decisions/02.json", output)
                self.assertEqual(decision["report"]["outcome"], "rejected")
                self.assertEqual(decision["assessment_errors"], [])
                stages = [event["payload"] for event in decision["events"]
                          if event["type"] == "proposal.assessed" and event["payload"].get("phase") in ("requirement", "evaluation")]
                self.assertTrue(stages)
                self.assertTrue(all(stage["error"] is None for stage in stages))
                if verdict != "requirement_uncertain":
                    self.assertEqual(stages[-1]["assessment"]["verdict"], verdict)
                self.assertEqual(decision["state"]["intentions"]["assessed_notes"]["status"], "active")
                self.assertEqual(result["actual_model_calls"], 0)

    def test_one_repair_reuses_prefix_and_baselines_with_physical_budget(self):
        original = self.root / "original-run"
        old = self.synthetic_original(original)
        before = self.snapshot_files(original)
        output = self.root / "repair"
        constructed, requested = [], []

        def neutral(cell, transport):
            constructed.append(cell["cell"])
            scripted = series.ScriptedProvider()

            class Neutral:
                name = "scripted"

                def propose(self, request):
                    requested.append(cell["cell"])
                    transport(("docker", "fake-model"), "never-auth", 5, ("docker", "kill", "fake"))
                    scripted.queue(series.Reply((series.rest(),)))
                    return scripted.propose(request)
            return Neutral()

        with patch.object(series, "subprocess_runner", return_value=RunResult(0)) as run:
            result = series.run_series(output, mode="live", auth_file="/external/not-read", repair_from=original, provider_factory=neutral)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual((result["inherited_decisions_started"], result["new_decisions_started"], result["decisions_started"]), (18, 42, 60))
        self.assertEqual((result["inherited_failed_decisions"], result["selected_logical_decisions"]), (2, 58))
        self.assertEqual((result["inherited_model_calls"], result["new_actual_model_calls"], result["actual_model_calls"]), (19, 42, 61))
        self.assertEqual((result["typed_requests"], run.call_count), (61, 42))
        self.assertEqual((len(result["cells"]), len(constructed), len(requested)), (18, 14, 42))
        self.assertFalse(set(constructed) & {cell["cell"] for cell in old["cells"][:4]})
        self.assertEqual(requested.count("return_unfinished-main-1"), 2)
        self.assertEqual(before, self.snapshot_files(original))
        manifest = self.read("manifest.json", output)
        self.assertEqual(manifest["repair_count"], 1)
        self.assertEqual(manifest["new_decisions_max"], 42)
        self.assertEqual(manifest["selected_logical_decisions_max"], 58)
        self.assertEqual(manifest["repair_of"]["manifest_sha256"], before["manifest.json"])
        self.assertTrue(manifest["repair_of"]["prefix_had_no_appraisals"])
        self.assertTrue(manifest["repair_of"]["world_guidance_source_unchanged"])
        self.assertTrue(manifest["repair_of"]["free_choice_disclosure_unchanged"])
        self.assertIn("runtime/world/cycle.py", manifest["repair_of"]["unchanged_source_sha256"])
        self.assertEqual([row["new_max_decisions"] for row in manifest["plan"][:5]], [0, 0, 0, 0, 2])
        repaired_cell = result["cells"][4]
        self.assertEqual(repaired_cell["session_usage"]["max_calls"], 7)
        self.assertEqual(repaired_cell["session_usage"]["max_ms"], series.CELL_MS - old["cells"][4]["session_usage"]["elapsed_ms"])
        self.assertEqual([row["outcome"] for row in result["inherited_failed_history"]["decisions"]], ["done", "rejected"])
        for family in series.FAMILIES:
            for name in ("baseline.db", "fixture.json"):
                relative = f"baselines/{family}/{name}"
                self.assertEqual(series.sha(output / relative), before[relative])
        for relative, digest in manifest["repair_of"]["artifact_sha256"].items():
            self.assertEqual(series.sha(output / "original" / relative), digest)
        for cell in result["cells"][:4]:
            name = cell["cell"]
            self.assertIn("reused_from", cell)
            self.assertEqual(series.sha(output / f"cells/{name}/persona.db"), before[f"cells/{name}/persona.db"])
        calls = self.read("calls.json", output)
        self.assertEqual(calls["attempts"][:19], self.read("calls.json", original)["attempts"])
        self.assertEqual(calls["attempts"][-1]["index"], 61)
        with self.assertRaises(ValueError):
            series.run_series(self.root / "second", mode="live", auth_file="/external/not-read", repair_from=output, provider_factory=neutral)
        self.assertFalse((self.root / "second").exists())

    def test_repair_refuses_invalid_history_unsafe_paths_and_changed_core(self):
        original = self.root / "original-run"
        self.synthetic_original(original)
        before = self.snapshot_files(original)

        def write_json(root, relative, transform):
            value = self.read(relative, root)
            transform(value)
            (root / relative).write_text(json.dumps(value, ensure_ascii=False))

        def refresh_manifest(root):
            write_json(root, "summary.json", lambda value: value.update(manifest_sha256=series.sha(root / "manifest.json")))

        cases = ("second", "status", "counter", "baseline_path", "prefix_appraise", "symlink", "core")
        for label in cases:
            with self.subTest(label=label):
                source = self.root / ("invalid-" + label)
                shutil.copytree(original, source)
                if label == "second":
                    write_json(source, "manifest.json", lambda value: value.update(repair_count=1))
                    refresh_manifest(source)
                elif label == "status":
                    write_json(source, "summary.json", lambda value: value.update(status="completed"))
                elif label == "counter":
                    write_json(source, "calls.json", lambda value: value.update(decisions_started=17))
                elif label == "baseline_path":
                    write_json(source, "manifest.json", lambda value: value["baselines"]["interest_emergence"].update(db="../../outside.db"))
                    refresh_manifest(source)
                elif label == "prefix_appraise":
                    write_json(source, "cells/interest_emergence-main-1/requests/001.json", lambda value: value["request"].update(purpose="appraise"))
                elif label == "symlink":
                    target = source / "cells/interest_emergence-main-1/requests/001.json"
                    target.unlink()
                    target.symlink_to(original / "cells/interest_emergence-main-1/requests/001.json")
                else:
                    write_json(source, "manifest.json", lambda value: value["sources"].update({"runtime/world/cycle.py": "0" * 64}))
                    refresh_manifest(source)
                output = self.root / ("out-" + label)
                with patch.object(series, "subprocess_runner", side_effect=AssertionError("No live traffic")) as run:
                    if label == "core":
                        result = series.run_series(output, mode="live", auth_file="/external/not-read", repair_from=source)
                        self.assertEqual(result["status"], "partial")
                        self.assertEqual(result["new_decisions_started"], 0)
                    else:
                        with self.assertRaises(ValueError):
                            series.run_series(output, mode="live", auth_file="/external/not-read", repair_from=source)
                        self.assertFalse(output.exists())
                run.assert_not_called()
        link = self.root / "source-link"
        link.symlink_to(original, target_is_directory=True)
        for source, output in ((link, self.root / "out-link"), (original, original / "nested-output")):
            with self.assertRaises(ValueError):
                series.run_series(output, mode="live", auth_file="/external/not-read", repair_from=source)
            self.assertFalse(output.exists())
        with self.assertRaises(ValueError):
            series.run_series(self.root / "auth-in-original", mode="live", auth_file=original / "iam-managed.json", repair_from=original)
        self.assertFalse((self.root / "auth-in-original").exists())
        self.assertEqual(before, self.snapshot_files(original))

    def test_inherited_call_cap_and_reduced_session_forecast_are_enforced(self):
        class Controller:
            session_state = "running"

        class Session:
            ctl = Controller()
            request_id = "req_InheritedTest01"
            calls = 0

            def usage(self):
                return dict(calls=self.calls, elapsed_ms=20, max_calls=7, max_ms=100)

            def check_limits(self):
                pass

            def charge(self):
                self.calls += 1

        artifacts = series.Artifacts(self.root)
        ledger = series.CallLedger(artifacts, attempts=[dict(index=n) for n in range(1, 240)], decisions=18)
        session = Session()
        with self.assertRaisesRegex(series.BudgetStop, "global_call_forecast"):
            ledger.forecast(session, live=True)
        ledger.charge(session, "repaired")
        with self.assertRaisesRegex(series.BudgetStop, "global_calls"):
            ledger.charge(session, "repaired")
        self.assertEqual((len(ledger.attempts), session.calls), (240, 1))
        with self.assertRaises(ValueError):
            series.CallLedger(artifacts, attempts=[{}] * 241)
        ledger = series.CallLedger(artifacts, decisions=18)
        session.calls = 4
        with self.assertRaisesRegex(series.BudgetStop, "cell_call_forecast"):
            ledger.forecast(session, live=True)

    def test_final_chain_reuses_five_cells_and_keeps_two_histories(self):
        original, second, output = self.root / "live1", self.root / "live2", self.root / "final"
        previous = self.synthetic_second(original, second)
        old1, old2 = self.snapshot_files(original), self.snapshot_files(second)
        constructed, requested = [], []
        with patch.object(series, "subprocess_runner", return_value=RunResult(0)) as run:
            result = series.run_series(output, mode="live", auth_file="/external/not-read", final_repair_from=second,
                                       provider_factory=self.neutral_factory(constructed=constructed, requested=requested))
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual((result["inherited_decisions_started"], result["new_decisions_started"], result["decisions_started"]), (22, 38, 60))
        self.assertEqual((result["inherited_model_calls"], result["new_actual_model_calls"], result["actual_model_calls"]), (23, 38, 61))
        self.assertEqual((result["inherited_failed_decisions"], result["selected_logical_decisions"]), (4, 56))
        self.assertEqual((run.call_count, len(constructed), len(result["cells"])), (38, 13, 18))
        self.assertFalse(set(constructed) & {row["cell"] for row in previous["cells"][:5]})
        self.assertEqual(requested.count("return_unfinished-main-2"), 2)
        self.assertEqual(result["cells"][5]["session_usage"]["max_calls"], 8)
        self.assertEqual(result["cells"][5]["session_usage"]["max_ms"], series.CELL_MS - previous["cells"][5]["session_usage"]["elapsed_ms"])
        self.assertEqual([[d["outcome"] for d in h["decisions"]] for h in result["inherited_failed_histories"]], [["done", "rejected"], ["done", "no_decision"]])
        self.assertEqual(result["cells"][0]["reused_from"], previous["cells"][0]["reused_from"])
        self.assertIn("reused_in_final_from", result["cells"][0])
        self.assertEqual([row["source_version"] for row in result["cells"][:5]], ["s22-series-1"] * 4 + ["s22-series-2"])
        manifest = self.read("manifest.json", output)
        self.assertEqual(manifest["repair_count"], 2)
        self.assertEqual(manifest["new_decisions_max"], 38)
        self.assertEqual(manifest["selected_logical_decisions_max"], 56)
        self.assertEqual([v["version"] for v in manifest["repair_of"]["source_versions"]], ["s22-series-1", "s22-series-2"])
        self.assertEqual(self.read("calls.json", output)["attempts"][:23], self.read("calls.json", second)["attempts"])
        self.assertEqual(old1, self.snapshot_files(original))
        self.assertEqual(old2, self.snapshot_files(second))
        for relative, digest in manifest["repair_of"]["artifact_sha256"].items():
            self.assertEqual(series.sha(output / "original" / relative), digest)
        with self.assertRaises(ValueError):
            series.run_series(self.root / "another-final", mode="live", auth_file="/external/not-read", final_repair_from=output)
        with self.assertRaises(ValueError):
            series.run_series(self.root / "legacy-on-repair", mode="live", auth_file="/external/not-read", repair_from=second)

    def test_final_repair_rejects_broken_lineage_and_unsafe_paths(self):
        original, second = self.root / "live1", self.root / "live2"
        self.synthetic_second(original, second)
        before1, before2 = self.snapshot_files(original), self.snapshot_files(second)

        def change(root, relative, transform):
            value = self.read(relative, root)
            transform(value)
            (root / relative).write_text(json.dumps(value, ensure_ascii=False))

        def refresh(root):
            change(root, "summary.json", lambda value: value.update(manifest_sha256=series.sha(root / "manifest.json")))

        for label in ("count", "counters", "inherited_ledger", "transitive", "null_response", "prefix", "inventory", "symlink"):
            with self.subTest(label=label):
                source = self.root / ("bad-" + label)
                shutil.copytree(second, source)
                if label == "count":
                    change(source, "manifest.json", lambda v: v.update(repair_count=2))
                    refresh(source)
                elif label == "counters":
                    change(source, "summary.json", lambda v: v.update(typed_requests=24))
                elif label == "inherited_ledger":
                    change(source, "calls.json", lambda v: v["attempts"][0].update(request_id="req_wrongInherited01"))
                elif label == "transitive":
                    change(source, "manifest.json", lambda v: v["repair_of"]["artifact_sha256"].update({"manifest.json": "0" * 64}))
                    refresh(source)
                elif label == "null_response":
                    change(source, "cells/return_unfinished-main-2/requests/002.json", lambda v: v.update(response={}))
                elif label == "prefix":
                    change(source, "cells/interest_emergence-main-1/requests/001.json", lambda v: v["request"].update(purpose="appraise"))
                elif label == "inventory":
                    change(source, "manifest.json", lambda v: v["sources"].update({"runtime/unknown.py": "0" * 64}))
                    refresh(source)
                else:
                    target = source / "original/manifest.json"
                    target.unlink()
                    target.symlink_to(original / "manifest.json")
                with patch.object(series, "subprocess_runner", side_effect=AssertionError("No live calls")) as run:
                    with self.assertRaises(ValueError):
                        series.run_series(self.root / ("out-" + label), mode="live", auth_file="/external/not-read", final_repair_from=source)
                run.assert_not_called()
        for kwargs in (dict(repair_from=original, final_repair_from=second),
                       dict(final_repair_from=second, auth_file=original / "iam-managed.json"),
                       dict(final_repair_from=second, auth_file=second / "iam-managed.json")):
            with self.assertRaises(ValueError):
                series.run_series(self.root / "unsafe-output", mode="live", **dict(dict(auth_file="/external/not-read"), **kwargs))
        with self.assertRaises(ValueError):
            series.run_series(second / "nested", mode="live", auth_file="/external/not-read", final_repair_from=second)
        frozen = series.source_hashes()
        frozen["runtime/intentions/completion.py"] = "0" * 64
        with patch.object(series, "source_hashes", return_value=frozen), patch.object(series, "subprocess_runner", side_effect=AssertionError("No calls")):
            result = series.run_series(self.root / "changed-core", mode="live", auth_file="/external/not-read", final_repair_from=second)
        self.assertEqual((result["status"], result["new_decisions_started"]), ("partial", 0))
        self.assertEqual(before1, self.snapshot_files(original))
        self.assertEqual(before2, self.snapshot_files(second))

    def test_official_native_repair_counts_two_calls_for_one_typed_request(self):
        from iam.providers.codex import fake
        calls = 0
        valid = fake.reply([dict(kind="no_action", rationale="Отдых", cites=[], body=dict(reason="Достаточно"))])

        def transport(*args):
            nonlocal calls
            calls += 1
            return fake.garbage() if calls == 1 else valid

        def provider(cell, runner):
            return series.CodexProvider(runner, series.CodexSandboxConfig(), model=series.MODEL, max_format_retries=1)

        with patch.object(series, "subprocess_runner", side_effect=transport):
            result = series.run_series(self.root / "run", mode="live", auth_file="/external/not-read", provider_factory=provider)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual((result["decisions_started"], result["typed_requests"], result["actual_model_calls"]), (60, 60, 61))
        attempts = self.read("calls.json")["attempts"]
        self.assertEqual(attempts[0]["request_id"], attempts[1]["request_id"])
        entry = self.read("cells/interest_emergence-main-1/decisions/01.json")
        self.assertEqual(entry["shared_format_budget"]["repairs"], ["native"])
        self.assertEqual(entry["shared_format_budget"]["physical_invocations"], 2)

    def test_native_and_cycle_share_one_repair_and_hard_cap_rejects_fifth(self):
        for ordering in ("native_cycle", "cycle_native", "cap"):
            with self.subTest(ordering=ordering):
                def factory(cell, transport):
                    scripted = series.ScriptedProvider()

                    class Mixed:
                        name = "codex"

                        def __init__(self):
                            self.count, self.last_calls, self.last_format_codes = 0, 0, []

                        def propose(self, request):
                            self.count += 1
                            self.last_calls, self.last_format_codes = 0, []
                            repetitions = 5 if ordering == "cap" else 2 if ordering == "native_cycle" or self.count > 1 else 1
                            for _ in range(repetitions):
                                transport(("docker", "fake-model"), "not-auth", 5, ("docker", "kill", "fake"))
                                if ordering != "cap":
                                    self.last_calls += 1
                                    self.last_format_codes = ["proposal_schema"]
                            scripted.queue(series.Reply((series.rest(),)))
                            response = scripted.propose(request)
                            response["invalid_extra"] = True
                            return response
                    return Mixed()

                output = self.root / ordering
                with patch.object(series, "subprocess_runner", return_value=RunResult(0)) as run:
                    result = series.run_series(output, mode="live", auth_file="/external/not-read", provider_factory=factory)
                expected = 4 if ordering == "cap" else 2
                self.assertEqual((result["status"], result["actual_model_calls"], result["decisions_started"]), ("partial", expected, 1), result)
                self.assertEqual(run.call_count, expected)
                self.assertEqual(result["cells"][0]["error"]["error_class"], "BudgetStop")
                if ordering != "cap":
                    trace = self.read("cells/interest_emergence-main-1/requests/002.json", output)
                    self.assertEqual(trace["format_codes"], ["proposal_schema"])

    def test_final_local_rejections_require_coverage_and_infra_stops_all(self):
        from iam.providers.errors import ProviderTimeout
        original, second = self.root / "live1", self.root / "live2"
        self.synthetic_second(original, second)
        cases = (("local", "interest_loss-main-1", 2, ("final_json",), ProviderFormatError, "completed_with_rejections"),
                 ("empty", "return_unfinished-main-2", 1, ("proposal_schema",), ProviderFormatError, "partial"),
                 ("unknown", "return_unfinished-main-2", 2, ("not-a-fixed-code",), ProviderFormatError, "partial"),
                 ("stream", "return_unfinished-main-2", 2, ("stream_jsonl",), ProviderFormatError, "partial"),
                 ("infra", "return_unfinished-main-2", 2, ("final_json",), ProviderTimeout, "partial"))
        for label, cell, at, codes, error_type, status in cases:
            with self.subTest(label=label):
                output = self.root / label
                with patch.object(series, "subprocess_runner", return_value=RunResult(0)):
                    result = series.run_series(output, mode="live", auth_file="/external/not-read", final_repair_from=second,
                                               provider_factory=self.neutral_factory(fail_cell=cell, fail_at=at, codes=codes, error_type=error_type))
                self.assertEqual(result["status"], status, result)
                self.assertIsNone(result["behavioral_success"])
                if label in ("local", "empty"):
                    self.assertEqual(len(result["cells"]), 18)
                    self.assertEqual(len(result["local_rejections"]), 1)
                    self.assertEqual(result["stop"], "no_valid_decision" if label == "empty" else None)
                else:
                    self.assertLess(len(result["cells"]), 18)
                    self.assertEqual(result["local_rejections"], [])
                if label == "unknown":
                    trace = self.read("cells/return_unfinished-main-2/requests/002.json", output)
                    self.assertEqual(trace["format_codes"], ["unsupported_format_code"])

    def test_final_malformed_assessment_is_cell_local_after_valid_choice(self):
        original, second = self.root / "live1", self.root / "live2"
        self.synthetic_second(original, second)
        underlying = self.appraisal_factory("malformed", target="interest_loss-main-1")

        def factory(cell, transport):
            inner = underlying(cell, transport)

            class Counted:
                name = inner.name

                def propose(self, request):
                    transport(("docker", "fake-model"), "not-auth", 5, ("docker", "kill", "fake"))
                    return inner.propose(request)
            return Counted()

        with patch.object(series, "subprocess_runner", return_value=RunResult(0)):
            result = series.run_series(self.root / "final", mode="live", auth_file="/external/not-read", final_repair_from=second, provider_factory=factory)
        self.assertEqual(result["status"], "completed_with_rejections", result)
        self.assertEqual(len(result["cells"]), 18)
        self.assertEqual(result["local_rejections"][0]["stop"], "assessment_error")
        self.assertEqual(result["local_rejections"][0]["error"]["assessments"][0]["error"], "assessment_format_or_cites")

    def test_final_flag_only_tool_response_stops_before_next_cell(self):
        original, second = self.root / "live1", self.root / "live2"
        self.synthetic_second(original, second)
        for detected in (True, False):
            with self.subTest(tool_use_detected=detected):
                constructed = []

                def factory(cell, transport):
                    constructed.append(cell["cell"])
                    scripted = series.ScriptedProvider()

                    class FlagOnly:
                        name = "codex"
                        count = 0

                        def propose(self, request):
                            self.count += 1
                            transport(("docker", "fake-model"), "not-auth", 5, ("docker", "kill", "fake"))
                            if cell["cell"] == "return_unfinished-main-2" and self.count == 2:
                                scripted.queue(series.Reply((), tool_use=detected))
                            else:
                                scripted.queue(series.Reply((series.rest(),)))
                            return scripted.propose(request)
                    return FlagOnly()

                output = self.root / ("tool" if detected else "ordinary-rejection")
                with patch.object(series, "subprocess_runner", return_value=RunResult(0)):
                    result = series.run_series(output, mode="live", auth_file="/external/not-read", final_repair_from=second, provider_factory=factory)
                if detected:
                    self.assertEqual((result["status"], result["stop"]), ("partial", "tool_use"), result)
                    self.assertEqual(constructed, ["return_unfinished-main-2"])
                    self.assertEqual((len(result["cells"]), result["new_decisions_started"]), (6, 2))
                    self.assertEqual(result["local_rejections"], [])
                    trace = self.read("cells/return_unfinished-main-2/requests/002.json", output)
                    self.assertTrue(trace["response"]["tool_use_detected"])
                    self.assertEqual(trace["response"]["proposals"], [])
                else:
                    self.assertEqual(result["status"], "completed", result)
                    self.assertEqual(len(result["cells"]), 18)
                    self.assertEqual(result["local_rejections"], [])

    def test_empty_response_is_not_a_choice_and_empty_only_cell_cannot_complete(self):
        request = dict(request_id="req_EmptyChoiceTest01", epoch=1, state_version=1, purpose="choose_action")
        scripted = series.ScriptedProvider([series.Reply(()), series.Reply((series.rest("Явно выбираю отдых"),))])
        empty = scripted.propose(request)
        self.assertEqual(series.jsonschema.errors("response", empty), [])
        self.assertFalse(series.valid_choice_trace(dict(request=request, response=empty, error=None)))
        rest = scripted.propose(request)
        self.assertTrue(series.valid_choice_trace(dict(request=request, response=rest, error=None)))

        original, second = self.root / "live1", self.root / "live2"
        self.synthetic_second(original, second)

        def factory(cell, transport):
            replies = series.ScriptedProvider()

            class EmptyCell:
                name = "scripted"

                def propose(self, query):
                    transport(("docker", "fake-model"), "not-auth", 5, ("docker", "kill", "fake"))
                    proposals = () if cell["cell"] == "return_unfinished-main-2" else (series.rest(),)
                    replies.queue(series.Reply(proposals))
                    return replies.propose(query)
            return EmptyCell()

        output = self.root / "final"
        with patch.object(series, "subprocess_runner", return_value=RunResult(0)):
            result = series.run_series(output, mode="live", auth_file="/external/not-read", final_repair_from=second, provider_factory=factory)
        self.assertEqual((result["status"], result["stop"], len(result["cells"])), ("partial", "no_valid_decision", 18), result)
        self.assertEqual(result["local_rejections"], [])
        empty_cell = result["cells"][5]
        self.assertEqual(empty_cell["cell"], "return_unfinished-main-2")
        self.assertEqual(empty_cell["valid_typed_choice_decisions"], 0)
        self.assertEqual([item["outcome"] for item in empty_cell["decisions"]], ["rejected", "rejected"])
        for number in (1, 2):
            trace = self.read(f"cells/return_unfinished-main-2/requests/{number:03d}.json", output)
            self.assertEqual(trace["response"]["proposals"], [])
            self.assertIsNone(trace["error"])
        self.assertIsNone(result["behavioral_success"])

    def first_action_factory(self, constructed):
        def factory(cell, transport):
            constructed.append(cell["cell"])
            replies = series.ScriptedProvider()

            class FirstAction:
                name = "scripted"
                count = 0

                def propose(self, query):
                    self.count += 1
                    transport(("docker", "fake-model"), "not-auth", 5, ("docker", "kill", "fake"))
                    proposal = (series.act(series.WRITE, dict(project_id="technical_probe", title="Проба", text="Текст локальной пробы."))
                                if cell["cell"] == "return_unfinished-main-2" and self.count == 1 else series.rest())
                    replies.queue(series.Reply((proposal,)))
                    return replies.propose(query)
            return FirstAction()
        return factory

    def test_final_action_crash_stops_despite_done_and_unknown_outcome(self):
        original, second = self.root / "live1", self.root / "live2"
        self.synthetic_second(original, second)
        constructed = []
        with patch.object(series, "subprocess_runner", return_value=RunResult(0)), \
                patch.object(series.PersistentWorld, "execute", side_effect=OSError("Local execution unavailable")):
            result = series.run_series(self.root / "final", mode="live", auth_file="/external/not-read", final_repair_from=second,
                                       provider_factory=self.first_action_factory(constructed))
        self.assertEqual((result["status"], result["stop"]), ("partial", "technical_fault"), result)
        self.assertEqual(constructed, ["return_unfinished-main-2"])
        self.assertEqual(result["local_rejections"], [])
        entry = self.read("cells/return_unfinished-main-2/decisions/01.json", self.root / "final")
        self.assertEqual(entry["report"]["outcome"], "done")
        self.assertEqual(entry["report"]["actions"][0]["status"], "unknown_outcome")
        self.assertTrue(any(f.get("event", {}).get("payload", {}).get("fault") == "action_crash" for f in entry["technical_faults"]))
        self.assertNotIn("technical_probe", entry["state"]["world_time"]["world"]["projects"])
        self.assertFalse(result["cells"][-1].get("planned_budget_stop", False))

    def test_final_critical_failed_stages_cannot_be_masked_by_limit_or_local(self):
        from iam.cycle.cycle import StageRecord
        original, second = self.root / "live1", self.root / "live2"
        self.synthetic_second(original, second)
        original_run = series.LivingCycle.run
        target = "return_unfinished-main-2"
        for stage, local in (("action", False), ("confirmation", False), ("memory_update", False), ("memory_update", True)):
            with self.subTest(stage=stage, local=local):
                calls = 0

                def run(owner, *args, **kwargs):
                    nonlocal calls
                    report = original_run(owner, *args, **kwargs)
                    calls += 1
                    if calls == 2:
                        report.stages.append(StageRecord(stage, "failed", (), owner.ctl.epoch, owner.ctl.state_version, {"status": "rejected"}))
                    return report

                output = self.root / (stage + ("-local" if local else "-limit"))
                provider = self.neutral_factory(fail_cell=target) if local else self.budget_boundary_factory(target)
                with patch.object(series, "subprocess_runner", return_value=RunResult(0)), patch.object(series.LivingCycle, "run", run):
                    result = series.run_series(output, mode="live", auth_file="/external/not-read", final_repair_from=second, provider_factory=provider)
                self.assertEqual((result["status"], result["stop"], len(result["cells"])), ("partial", "technical_fault", 6), result)
                self.assertEqual(result["local_rejections"], [])
                self.assertFalse(result["cells"][-1].get("planned_budget_stop", False))
                entry = self.read(f"cells/{target}/decisions/02.json", output)
                self.assertTrue(any(f.get("stage", {}).get("stage") == stage for f in entry["technical_faults"]))
                if not local:
                    self.assertEqual(entry["report"]["outcome"], "done")
                    self.assertFalse(any(e["type"] == "system.error" for e in entry["events"]))
                    self.assertEqual(result["cells"][-1]["session_usage"]["calls"], 8)

    def test_final_recorded_domain_failure_or_unknown_remains_negative(self):
        original, second = self.root / "live1", self.root / "live2"
        self.synthetic_second(original, second)
        for outcome in ("failed", "unknown"):
            with self.subTest(outcome=outcome):
                output = self.root / outcome
                with patch.object(series, "subprocess_runner", return_value=RunResult(0)), \
                        patch.object(series.PersistentWorld, "execute", return_value=series.ActionOutcome(outcome, "Обычный исход действия")):
                    result = series.run_series(output, mode="live", auth_file="/external/not-read", final_repair_from=second,
                                               provider_factory=self.first_action_factory([]))
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(len(result["cells"]), 18)
                entry = self.read("cells/return_unfinished-main-2/decisions/01.json", output)
                self.assertEqual(entry["report"]["actions"][0]["status"], "failed" if outcome == "failed" else "unknown_outcome")
                self.assertEqual(entry["technical_faults"], [])
                self.assertTrue(all(s["status"] == "ok" for s in entry["report"]["stages"] if s["stage"] in ("action", "confirmation", "memory_update")))
                self.assertNotIn("technical_probe", entry["state"]["world_time"]["world"]["projects"])

    def budget_boundary_factory(self, target, *, extra=False, quota=False):
        """Model the four-invocation decision envelope without real CLI traffic."""
        def factory(cell, transport):
            scripted = series.ScriptedProvider()

            class Costed:
                name = "scripted"
                count = 0

                def propose(self, request):
                    self.count += 1
                    cost = 4 if cell["cell"] == target else 1
                    if extra and cell["cell"] == target and self.count == 2:
                        cost = 5
                    for _ in range(cost):
                        transport(("docker", "fake-model"), "not-auth", 5, ("docker", "kill", "fake"))
                    if quota and cell["cell"] == target and self.count == 2:
                        from iam.providers.errors import ProviderLimitError
                        raise ProviderLimitError("External subscription limit")
                    scripted.queue(series.Reply((series.rest(),)))
                    response = scripted.propose(request)
                    response["usage"]["calls"] = cost
                    return response
            return Costed()
        return factory

    def test_final_planned_cell_forecast_and_exact_call_limit_continue(self):
        original, second = self.root / "live1", self.root / "live2"
        self.synthetic_second(original, second)
        for target, stop, cap in (("interest_loss-main-1", "cell_call_forecast", 10),
                                  ("return_unfinished-main-2", "cell_calls_exhausted", 8)):
            with self.subTest(stop=stop):
                output = self.root / stop
                with patch.object(series, "subprocess_runner", return_value=RunResult(0)):
                    result = series.run_series(output, mode="live", auth_file="/external/not-read", final_repair_from=second,
                                               provider_factory=self.budget_boundary_factory(target))
                self.assertEqual((result["status"], result["stop"], len(result["cells"])), ("completed", None, 18), result)
                cell = next(c for c in result["cells"] if c["cell"] == target)
                self.assertEqual(cell["stop"], stop)
                self.assertTrue(cell["planned_budget_stop"])
                self.assertEqual(cell["session_usage"]["max_calls"], cap)
                self.assertEqual(cell["session_usage"]["calls"], 8)
                self.assertGreaterEqual(cell["valid_typed_choice_decisions"], 1)
                self.assertEqual(len(cell["decisions"]), 2)
                self.assertEqual(result["local_rejections"], [])
                self.assertLessEqual(result["decisions_started"], 60)
                self.assertLessEqual(result["actual_model_calls"], 240)
                if stop == "cell_calls_exhausted":
                    decision = self.read(f"cells/{target}/decisions/02.json", output)
                    self.assertEqual(decision["planned_budget_stop"], stop)
                    self.assertEqual(decision["session_usage_after_limits"]["calls"], 8)
                    self.assertTrue(any(e["type"] == "session.paused" and e["payload"]["reason"] == "session_limit" for e in decision["limit_events"]))

    def test_final_attempted_extra_call_time_manual_pause_and_quota_stay_fatal(self):
        original, second = self.root / "live1", self.root / "live2"
        self.synthetic_second(original, second)
        original_usage, original_check = series.SessionRunner.usage, series.SessionRunner.check_limits
        target = "return_unfinished-main-2"
        for kind in ("extra", "time", "operator", "quota"):
            with self.subTest(kind=kind):
                def usage(owner):
                    value = original_usage(owner)
                    if getattr(owner, "_expired_for_test", False):
                        value["elapsed_ms"] = value["max_ms"]
                    return value

                def check(owner):
                    value = original_usage(owner)
                    if owner.ctl.session_state == "running" and value["calls"] == value["max_calls"]:
                        if kind == "time":
                            owner._expired_for_test = True
                        elif kind == "operator":
                            owner.ctl.pause_session("operator")
                    return original_check(owner)

                with patch.object(series, "subprocess_runner", return_value=RunResult(0)), \
                        patch.object(series.SessionRunner, "usage", usage), patch.object(series.SessionRunner, "check_limits", check):
                    result = series.run_series(self.root / kind, mode="live", auth_file="/external/not-read", final_repair_from=second,
                                               provider_factory=self.budget_boundary_factory(target, extra=kind == "extra", quota=kind == "quota"))
                self.assertEqual(result["status"], "partial", result)
                self.assertEqual(len(result["cells"]), 6)
                self.assertEqual(result["local_rejections"], [])
                cell = result["cells"][-1]
                self.assertFalse(cell.get("planned_budget_stop", False))
                self.assertLessEqual(cell["session_usage"]["calls"], 8)
                if kind == "extra":
                    self.assertEqual(cell["error"]["error_class"], "BudgetStop")
                elif kind == "quota":
                    self.assertEqual(cell["error"]["error_class"], "ProviderLimitError")
                else:
                    self.assertEqual(cell["stop"], "session_paused")

    def test_timeout_keeps_cleanup_and_stops_without_retry(self):
        def factory(cell, transport):
            class TimedOut:
                name = "codex"

                def propose(self, request):
                    result = transport(("docker", "fake-model"), "not-logged", 1, ("docker", "kill", "fake"))
                    self.assertion = result.timed_out
                    from iam.providers.errors import ProviderTimeout
                    raise ProviderTimeout("timeout")
            return TimedOut()

        with patch.object(series, "subprocess_runner", return_value=RunResult(None, timed_out=True)) as run:
            result = series.run_series(self.root / "run", mode="live", auth_file="/external/not-read", provider_factory=factory)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[3], ("docker", "kill", "fake"))
        attempt = self.read("calls.json")["attempts"][0]
        self.assertTrue(attempt["timed_out"])
        self.assertEqual(result["cells"][0]["error"]["error_class"], "ProviderTimeout")

    def test_official_configuration_and_cycle_format_repair_are_counted(self):
        credentials = self.root / "iam-managed.json"
        credentials.write_text("not-real-credentials-and-never-read")
        credentials.chmod(0o600)
        configurations = []

        class FakeOfficial:
            name = "codex"

            def __init__(self, runner, sandbox, **kwargs):
                configurations.append((sandbox, kwargs))
                self.runner, self.calls = runner, 0
                self.script = series.ScriptedProvider(name="codex", model=series.MODEL)

            def propose(self, request):
                self.runner(("docker", "fake-model"), "not-auth", 10, ("docker", "kill", "fake"))
                self.calls += 1
                self.script.queue(series.Reply((series.rest(),)))
                response = self.script.propose(request)
                if self.calls == 1:
                    response["invalid_extra_field"] = True
                return response

        with patch.object(series, "CodexProvider", FakeOfficial), patch.object(series, "subprocess_runner", return_value=RunResult(0)) as run:
            result = series.run_series(self.root / "run", mode="live", auth_file=credentials)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["decisions_started"], 60)
        self.assertEqual((result["actual_model_calls"], result["typed_requests"], run.call_count), (78, 78, 78))
        self.assertEqual(len(configurations), 18)
        for sandbox, settings in configurations:
            self.assertTrue(sandbox.persist_auth_updates)
            self.assertEqual(settings["model"], "gpt-6-luna")
            self.assertEqual(settings["max_format_retries"], 1)
        first = self.read("cells/interest_emergence-main-1/requests/001.json")["request"]
        repair = self.read("cells/interest_emergence-main-1/requests/002.json")["request"]
        self.assertEqual(repair["retry_of"], first["request_id"])
        self.assertEqual(repair["limits"]["attempt"], 1)
        entry = self.read("cells/interest_emergence-main-1/decisions/01.json")
        self.assertEqual(entry["technical_faults"], [])
        self.assertEqual(entry["resolved_format_events"][0]["repair"]["failed_request_id"], first["request_id"])
        self.assertEqual(entry["resolved_format_events"][0]["repair"]["repair_request_id"], repair["request_id"])
        unmatched = dict(entry["resolved_format_events"][0]["event"], idempotency_key="err:bad_response:unmatched")
        traces = [self.read(f"cells/interest_emergence-main-1/requests/{n:03d}.json") for n in (1, 2)]
        faults, resolved, _, _ = series.decision_technical_faults(entry["report"], [unmatched], traces)
        self.assertEqual(resolved, [])
        self.assertEqual(faults[0]["event"], unmatched)
        self.assertNotIn(str(credentials), (self.root / "run/manifest.json").read_text())
        self.assertEqual(credentials.read_text(), "not-real-credentials-and-never-read")

    def test_budget_forecast_and_pre_call_global_refusal(self):
        class Controller:
            session_state = "running"

        class Session:
            ctl = Controller()
            request_id = "req_TestLedger01"
            calls = 6

            def usage(self):
                return dict(calls=self.calls, elapsed_ms=0)

            def check_limits(self):
                pass

            def charge(self):
                self.calls += 1

        ledger = series.CallLedger(series.Artifacts(self.root))
        session = Session()
        ledger.forecast(session, live=True)
        session.calls = 7
        with self.assertRaisesRegex(series.BudgetStop, "cell_call_forecast"):
            ledger.forecast(session, live=True)
        session.calls = 0
        ledger.attempts = [{} for _ in range(237)]
        with self.assertRaisesRegex(series.BudgetStop, "global_call_forecast"):
            ledger.forecast(session, live=True)
        ledger.attempts = [{} for _ in range(240)]
        with self.assertRaisesRegex(series.BudgetStop, "global_calls"):
            ledger.charge(session, "test")
        self.assertEqual(session.calls, 0)
        ledger.attempts = []
        ledger.decisions = 60
        with self.assertRaisesRegex(series.BudgetStop, "global_decisions"):
            ledger.begin_decision()

    def test_new_output_and_safe_cli_are_required(self):
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "marker"
        marker.write_text("preserve")
        with self.assertRaises(FileExistsError):
            series.run_series(existing)
        self.assertEqual(marker.read_text(), "preserve")
        with self.assertRaises(ValueError):
            series.run_series(self.root / "missing-auth", mode="live")
        with self.assertRaises(ValueError):
            series.run_series(self.root / "bad-auth", mode="live", auth_file=self.root / "bad-auth/auth.json")
        self.assertFalse((self.root / "missing-auth").exists())
        self.assertFalse((self.root / "bad-auth").exists())
        for args in (["--mode", "live"], ["--mode", "stub", "--auth-file", "/external/no-read"]):
            done = subprocess.run([sys.executable, str(SCRIPT), "--output", str(self.root / "cli"), *args], capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertFalse((self.root / "cli").exists())


if __name__ == "__main__":
    unittest.main()
