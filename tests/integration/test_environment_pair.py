import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from iam.providers.run import RunResult
from iam.world import fingerprint
from integration import environment_pair as pair


class EnvironmentPairTest(unittest.TestCase):
    def test_fixed_schedule_and_budget(self):
        self.assertEqual(pair.SCHEDULE, ('empty-1', 'library-1', 'empty-2', 'library-2',
                                         'empty-3', 'library-3', 'empty-4', 'library-4'))
        self.assertEqual((pair.CALLS_PER_SESSION, pair.MAX_CALLS, pair.SESSION_MS), (6, 48, 1_800_000))

    def test_unread_body_or_fingerprint_is_blocked_before_model(self):
        body = 'Содержимое ещё не прочитанного материала'
        material = {'text': body, 'fingerprint': fingerprint(body), 'read': False}
        provider = pair.PairProvider(Mock(), {'world': SimpleNamespace(state={'library': {'one': material}}),
                                             'materials': ['one']})
        for forbidden in (body, material['fingerprint']):
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(pair.StopSeries, 'unread_material_exposed'):
                provider.propose({'context': [{'text': forbidden}]})
        provider.inner.propose.assert_not_called()

    def test_reserves_physical_calls_before_runner_and_rejects_seventh_call(self):
        summary = {'attempts': []}
        session = Mock()
        session.usage.return_value = {'elapsed_ms': 0}
        output = Mock()
        transport = pair.call_transport(Path('/unused'), {}, summary, 'empty-1', session, output)
        def runner(*args):
            self.assertEqual(summary['attempts'][-1]['status'], 'reserved')
            self.assertEqual(session.charge.call_count, len(summary['attempts']))
            self.assertEqual(args[2], 60)
            return RunResult(0)
        with (patch.object(pair, 'check_frozen'), patch.object(pair, 'subprocess_runner', side_effect=runner) as invoke,
              patch.object(pair, 'record_answer', return_value={'tool_use_categories': [], 'redactions': 0})):
            stdin = json.dumps({'auth': {}})
            for _ in range(6):
                transport(('fixture',), stdin, 90)
            with self.assertRaisesRegex(pair.StopSeries, 'physical_call_cap'):
                transport(('fixture',), stdin, 90)
        self.assertEqual(invoke.call_count, 6)
        self.assertEqual([row['index'] for row in summary['attempts']], list(range(1, 7)))
        self.assertTrue(all(row['status'] == 'returned' for row in summary['attempts']))

    def test_secret_in_physical_answer_stops_before_typed_application(self):
        summary = {'attempts': []}
        session = Mock()
        session.usage.return_value = {'elapsed_ms': 0}
        transport = pair.call_transport(Path('/unused'), {}, summary, 'empty-1', session, Mock())
        with (patch.object(pair, 'check_frozen'), patch.object(pair, 'subprocess_runner', return_value=RunResult(0)),
              patch.object(pair, 'record_answer', return_value={'tool_use_categories': [], 'redactions': 1}),
              self.assertRaisesRegex(pair.StopSeries, 'secret_in_physical_answer')):
            transport(('fixture',), json.dumps({'auth': {}}), 60)
        self.assertEqual(len(summary['attempts']), 1)
