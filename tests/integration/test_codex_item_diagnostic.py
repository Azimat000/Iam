import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from iam.providers.classify import Kind
from iam.providers.codex import classify, fake
from iam.providers.run import RunResult
from integration import codex_item_diagnostic as diagnostic
from providers.test_codex import AUTH, request
from providers.test_codex_auth_session import handoff


class CodexItemDiagnosticTest(unittest.TestCase):
    def test_shape_summary_preserves_classification_and_excludes_private_fields(self):
        events = [
            {'type': 'item.started', 'item': {'text': 'PRIVATE_MISSING'}},
            {'type': 'item.updated', 'item': {'type': None, 'id': 'PRIVATE_ID'}},
            {'type': 'item.completed', 'item': {'type': ['PRIVATE_ARRAY']}},
            {'type': 'item.completed', 'item': {'type': 'PRIVATE_CUSTOM_TYPE', 'command': 'PRIVATE_COMMAND'}},
            {'type': 'item.completed', 'item': {'type': 'web_search', 'query': 'PRIVATE_QUERY'}},
            {'type': 'item.completed', 'item': {'type': 'error', 'message': 'PRIVATE_WARNING'}},
            {'type': 'item.completed', 'item': {'type': 'error', 'message': 'model rerouted: PRIVATE_MODEL_INFO'}},
            {'type': 'item.completed', 'item': {'type': 'error', 'message': ['PRIVATE_ERROR_ARRAY']}},
            {'type': 'item.completed', 'item': {'type': 'collab_tool_call', 'prompt': 'PRIVATE_COLLAB'}},
            {'type': 'PRIVATE_MCP_EVENT', 'text': 'PRIVATE_EVENT_DATA'},
            {'type': 'iam.auth.updated', 'auth': AUTH},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'PRIVATE_ANSWER'}},
        ]
        run = RunResult(0, '\n'.join(json.dumps(event) for event in events + events[:1]), 'PRIVATE_STDERR')
        summary = diagnostic.inspect_stream(run)
        actual = {(item['category'], item['phase'], item['encoding'], item['public_type'], item['message_shape'])
                  for item in summary['findings']}
        self.assertEqual(actual, {
            ('unknown_item_type', 'started', 'missing', None, None),
            ('unknown_item_type', 'updated', 'null', None, None),
            ('unknown_item_type', 'completed', 'non_string', None, None),
            ('unknown_item_type', 'completed', 'string', None, None),
            ('known_tool_item', 'completed', 'string', 'web_search', None),
            ('unknown_item_type', 'completed', 'string', 'error', 'other_message'),
            ('unknown_item_type', 'completed', 'string', 'error', 'model_reroute_prefix'),
            ('unknown_item_type', 'completed', 'string', 'error', 'missing_or_non_string'),
            ('unknown_item_type', 'completed', 'string', 'collab_tool_call', None),
            ('event_type_pattern', 'other', 'not_applicable', None, None),
        })
        self.assertEqual(len(summary['findings']), len(actual))
        self.assertEqual(classify(run).kind, Kind.TOOL_USE)
        self.assertNotIn('PRIVATE', json.dumps(summary))
        self.assertNotIn(AUTH['tokens']['access_token'], json.dumps(summary))

    def test_one_shot_uses_only_a_frozen_request_and_records_no_raw_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, auth, preflight = (root / name for name in ('trace.json', 'external.json', 'preflight.json'))
            typed = request('appraise', allowed_proposals=['intention'], cycle_id='cyc_Provider0001',
                            response_schema='response.schema.json', idempotency_key='req:req_Provider0001')
            typed.pop('unread')
            typed['context'][0].pop('extra')
            source.write_text(json.dumps({'request': typed}))
            auth.write_text(json.dumps(AUTH))
            auth.chmod(0o600)
            preflight.write_text(json.dumps(dict(image=diagnostic.IMAGE, model_calls=0, isolation_passed=True,
                                                 schema_matches=True, launcher_matches=True)))
            valid_digest = diagnostic.sha(source)
            base = fake.reply([], 'PRIVATE_FINAL_BODY')
            event = {'type': 'item.completed', 'item': {'type': 'PRIVATE_UNLISTED', 'text': 'PRIVATE_TEXT'}}
            run = handoff(RunResult(0, base.stdout + '\n' + json.dumps(event)), AUTH)
            output = root / 'result'
            with (patch.object(diagnostic, 'REQUEST_TRACE_SHA', valid_digest),
                  patch.object(diagnostic, 'subprocess_runner', return_value=run) as invoke):
                result = diagnostic.inspect_once(source, output, auth, preflight)
                self.assertEqual(result, {'status': 'blocked', 'tool_use_detected': True,
                                          'physical_calls': 1, 'applied': False})
                with self.assertRaises(FileExistsError):
                    diagnostic.inspect_once(source, output, auth, preflight)
                source.write_text('{}')
                with self.assertRaisesRegex(ValueError, 'unexpected_request_trace'):
                    diagnostic.inspect_once(source, root / 'other', auth, preflight)
                invoke.assert_called_once()
            self.assertEqual({path.name for path in output.iterdir()},
                             {'manifest.json', 'attempt.json', 'stream.json', 'summary.json'})
            for path in output.iterdir():
                self.assertNotIn('PRIVATE', path.read_text())
                self.assertNotIn(AUTH['tokens']['access_token'], path.read_text())

    def test_malformed_or_clean_stream_saves_no_arbitrary_data(self):
        self.assertEqual(diagnostic.inspect_stream(fake.reply([], 'PRIVATE_FINAL'))['findings'], [])
        run = RunResult(None, 'PRIVATE_GARBAGE\n[]\n{"type":[]}\n', timed_out=True)
        self.assertEqual(diagnostic.inspect_stream(run), {'returncode': None, 'timed_out': True,
                                                          'stderr_present': False, 'findings': []})
