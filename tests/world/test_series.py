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
from iam.providers.errors import ProviderUnavailable
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

        with patch.object(series, "subprocess_runner", return_value=RunResult(0)) as run:
            result = series.run_series(output, mode="live", auth_file="/external/not-read", provider_factory=counted_factory)
        self.assertEqual((result["status"], result["stop"], result["decisions_started"], result["actual_model_calls"]),
                         ("partial", "assessment_error", 18, 19), result)
        self.assertEqual(run.call_count, 19)
        return result

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
        self.assertEqual(manifest["budgets"]["provider_format_retries"], 0)
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
            self.assertEqual(settings["max_format_retries"], 0)
        first = self.read("cells/interest_emergence-main-1/requests/001.json")["request"]
        repair = self.read("cells/interest_emergence-main-1/requests/002.json")["request"]
        self.assertEqual(repair["retry_of"], first["request_id"])
        self.assertEqual(repair["limits"]["attempt"], 1)
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
