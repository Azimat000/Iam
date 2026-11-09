"""Механическая допустимость create, фильтр off, общий снимок и пределы диагностики."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from iam.domain import new_id
from iam.providers import CodexProvider, CodexSandboxConfig
from iam.providers.codex import fake
from iam.providers.run import RunResult
from iam.state import StateController
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act
from iam.world import READ, WRITE
from integration import intention_probe as probe
from integration.test_persistent_core import choose, wire


class IntentionProbeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="iam-intention-probe-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source, self.output = self.root / "source", self.root / "result"
        self.memories = {}
        for story in probe.STORIES:
            path = self.source / f"stories/{story}/persona.db"
            probe.series.initial_database(path, story)
            with Store.open(path) as store:
                ctl = StateController(store)
                session, cycle, provider = wire(ctl, cycle_type=probe.series.ExperimentCycle)
                session.command("start", "MECHANICAL FIXTURE ONLY")
                proposal = (act(READ, {"material_id": next(iter(cycle.world.state["library"]))}) if story == "A" else
                            act(WRITE, {"project_id": "draft", "title": "Зарисовка", "text": "На столе лежит стекло."}))
                report = choose(cycle, provider, proposal)
                self.assertEqual(report.actions[0].status, "confirmed")
                self.memories[story] = report.actions[0].memory_id
                session.command("stop", "END FIXTURE")

    def goal(self, story):
        return {"proposal_id": new_id("prop"), "kind": "intention", "rationale": "Механическая проверка",
                "cites": [self.memories[story]], "body": {"op": "create", "intention_id": "probe",
                "text": "Написать зарисовку о стекле", "criterion": "В тексте есть описание света на стекле",
                "project_id": "draft", "operation": "write"}}

    def test_create_accepted_in_both_histories_and_hidden_basis_rejected_from_same_past(self):
        def factory(name, transport):
            return ScriptedProvider([Reply((self.goal(name[0]),))])
        result = probe.run_probe(self.source, self.output, provider_factory=factory)
        self.assertEqual((result["status"], result["decisions"], result["model_calls"]), ("completed", 6, 0))
        self.assertIsNone(result["readiness"])
        for story in probe.STORIES:
            cells = [cell for cell in result["cells"] if cell["name"].startswith(story)]
            self.assertEqual(len({cell["before_digest"] for cell in cells}), 1)
            self.assertEqual([cell["new_intentions"] for cell in cells], [1, 1, 0])
            rejected = json.loads((self.output / f"cells/{story}-memory_off/result.json").read_text())
            self.assertEqual(rejected["report"]["outcome"], "rejected")
            request = rejected["traces"][0]["request"]
            self.assertNotIn(self.memories[story], json.dumps(request))
            for item in request["context"]:
                self.assertNotIn(item["layer"], ("personal", "interpretation"))
        with self.assertRaises(ValueError):
            probe.run_probe(self.source, self.output)

    def test_empty_proposals_are_valid_observation_without_an_intention(self):
        result = probe.run_probe(self.source, self.output,
                                provider_factory=lambda name, transport: ScriptedProvider([Reply(())]))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(all(cell["new_intentions"] == 0 for cell in result["cells"]))

    def test_every_physical_answer_survives_schema_failure_without_retry(self):
        def factory(name, transport):
            return CodexProvider(transport, CodexSandboxConfig(), max_format_retries=0)
        invalid = fake.events({"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
                              {"type": "item.completed", "item": {"id": "i", "type": "agent_message",
                               "text": '{"proposals":[{"kind":"intention","body_json":"bad"}],"explanation":"x"}'}},
                              {"type": "turn.completed"})
        with patch.object(probe, "subprocess_runner", return_value=invalid) as runner:
            result = probe.run_probe(self.source, self.output, mode="live", auth_file=str(self.root / "private.json"),
                                     provider_factory=factory)
        self.assertEqual((result["status"], result["model_calls"], runner.call_count), ("stopped", 1, 1))
        record = json.loads((self.output / "cells/A-baseline/physical/001.json").read_text())
        self.assertIn('"kind":"intention"', record["messages"][0])
        request = json.loads((self.output / "cells/A-baseline/requests/001.json").read_text())
        self.assertEqual(request["format_codes"], ["proposal_schema"])

    def test_physical_call_cap_and_reservation_precede_transport(self):
        class OverCalls:
            name = "test"
            def __init__(self, transport):
                self.transport = transport
            def propose(self, request):
                for _ in range(3):
                    self.transport(("fake",), "PRIVATE_STDIN", 1)
                raise AssertionError("Third transport must not be reached")
        with patch.object(probe, "subprocess_runner", return_value=RunResult(0)) as runner:
            result = probe.run_probe(self.source, self.output, mode="live", auth_file=str(self.root / "private.json"),
                                     provider_factory=lambda name, transport: OverCalls(transport))
        self.assertEqual((result["status"], result["model_calls"], runner.call_count), ("stopped", 2, 2))
        self.assertEqual(len(list(self.output.rglob("physical/*.json"))), 2)
        self.assertTrue(all(attempt["status"] == "returned" for attempt in result["attempts"]))
        for file in self.output.rglob("*.json"):
            self.assertNotIn("PRIVATE_STDIN", file.read_text())

    def test_changed_sources_stop_before_the_first_choice(self):
        original = probe.frozen_sources()
        with patch.object(probe, "frozen_sources", side_effect=[original, {**original, "changed": "yes"}]):
            result = probe.run_probe(self.source, self.output)
        self.assertEqual((result["status"], result["decisions"], result["model_calls"]), ("stopped", 0, 0))
        self.assertEqual(result["diagnostic"], "frozen_sources_changed")
