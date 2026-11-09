"""Отклонённый формат сохраняется; секреты и служебный поток не выходят в артефакт."""
import json
from pathlib import Path
import tempfile
import unittest

from iam.providers.run import RunResult
from integration.answer_audit import record_answer
from integration.live_series import audit


class PhysicalAnswerTest(unittest.TestCase):
    def test_tool_categories_match_classifier_and_never_store_private_event_fields(self):
        from iam.providers.codex.classify import classify
        from iam.providers.codex import fake
        from iam.providers.classify import Kind
        cases = [
            ({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'PRIVATE_COMMAND'}},
             ['known_tool_item']),
            ({'type': 'item.completed', 'item': {'type': 'PRIVATE_UNKNOWN', 'text': 'PRIVATE_BODY'}},
             ['unknown_item_type']),
            ({'type': 'PRIVATE_TOOL_EVENT', 'message': 'PRIVATE_MESSAGE'}, ['event_type_pattern']),
            ({'type': 'iam.auth.updated', 'auth': {'tokens': {'access_token': 'PRIVATE_TOKEN'}}}, []),
            ({'type': None, 'secret': 'PRIVATE_TYPE'}, []),
        ]
        with tempfile.TemporaryDirectory() as directory:
            artifacts = audit.Artifacts(directory)
            for index, (event, expected) in enumerate(cases, 1):
                run = RunResult(0, fake.reply([]).stdout + '\n' + json.dumps(event))
                result = record_answer(artifacts, 'cell', index, run)
                self.assertEqual(result['tool_use_categories'], expected)
                self.assertEqual(classify(run).kind == Kind.TOOL_USE, bool(expected))
                saved = (Path(directory) / f'cell/physical/{index:03d}.json').read_text()
                self.assertNotIn('PRIVATE', saved)
                self.assertTrue(result['body_available'])
            clean = record_answer(artifacts, 'cell', len(cases) + 1, fake.reply([]))
            self.assertEqual(clean['tool_use_categories'], [])

    def test_invalid_model_body_is_preserved_without_credentials_or_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = audit.Artifacts(directory)
            token, refreshed = "prior-private-credential-8675309", "updated-private-credential-1234567"
            body = '{"proposals":[{"kind":"intention","body_json":"broken"}],"secret":"' + token + ' ' + refreshed + '"}'
            events = [{"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE_REASONING"}},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": body}},
                      {"type": "iam.auth.updated", "auth": {"tokens": {"refresh_token": refreshed}}}]
            run = RunResult(0, "\n".join(json.dumps(event) for event in events), "PRIVATE_STDERR")
            result = record_answer(artifacts, "cell", 1, run, auth={"tokens": {"access_token": token}})
            saved = (Path(directory) / "cell/physical/001.json").read_text()
            for private in (token, refreshed, "PRIVATE_REASONING", "PRIVATE_STDERR", "iam.auth.updated"):
                self.assertNotIn(private, saved)
            self.assertEqual(result["redactions"], 2)
            self.assertTrue(result["body_available"])
            self.assertIn('"kind":"intention"', result["messages"][0])
            self.assertIn('"body_json":"broken"', result["messages"][0])
            self.assertTrue(result["stderr_present"])
            with self.assertRaises(FileExistsError):
                record_answer(artifacts, "cell", 1, run)

    def test_malformed_stream_is_explicit_gap_and_malformed_final_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = audit.Artifacts(directory)
            result = record_answer(artifacts, "cell", 1, RunResult(None, "bad secret raw line\n[]", timed_out=True))
            self.assertFalse(result["body_available"])
            self.assertEqual(result["malformed_stream_lines"], 2)
            self.assertEqual(result["messages"], [])
            self.assertTrue(result["timed_out"])
            line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "{invalid JSON"}})
            result = record_answer(artifacts, "cell", 2, RunResult(0, line))
            self.assertEqual(result["messages"], ["{invalid JSON"])
