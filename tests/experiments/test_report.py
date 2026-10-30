"""S16: честный допуск, сравнение содержимого и отсутствие меток в слепом пакете."""
import copy
import csv
import hashlib
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from iam.experiments import Bench, Persona, Scenario
from iam.experiments.report import assess, rank, run_suite
from iam.experiments.report.__main__ import main
from iam.experiments.report.blind import MARKERS, build_blind, fragment
from iam.experiments.report.evaluation import evaluate, lookup, subject
from iam.experiments.report.suite import BoundedProvider, stub_factory, eligibility
from iam.experiments.results import RunResult
from iam.experiments.variants.model_led import ModelLedArchitecture
from iam.providers.errors import ProviderUnavailable, SessionLimitReached, ProviderFormatError
from iam.stubs import Reply, ScriptedProvider, rest

ROOT = Path(__file__).resolve().parents[2]


def event(i, kind, causes=(), payload=None):
    return {'event_id': i, 'type': kind, 'causes': list(causes), 'payload': payload or {}}


def sample(text='Хочу подумать о прочитанном.'):
    return {'architecture': 'A-model-led', 'journal': [event('accepted', 'proposal.accepted', payload={'proposal_id': 'p1'})], 'final_state': {'intentions': {'x': {'status': 'active'}}},
            'snapshots': [{'after': 'choose', 'state': {'v': 1}}],
            'run': 'one', 'decision_trace': [{'input': 'Чем заняться?', 'memory': [], 'requests': [],
                'proposals': [{'response': {'proposals': [{'proposal_id': 'p1', 'kind': 'no_action', 'body': {'reason': text}}]}}]}]}


class ReportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_gate_precedes_scores_and_missing_rating_is_not_invented(self):
        self.assertEqual(rank(['fail'], 5, 5), {'eligible': False, 'score': None})
        self.assertFalse(rank(['unverified'], 5, 5)['eligible'])
        self.assertFalse(rank([], 5, 5)['eligible'])
        self.assertEqual(rank(['pass'], 2, 4)['score'], 3)
        self.assertIsNone(rank(['pass'], None, 5)['score'])
        self.assertIsNone(rank(['pass'], 5, None)['score'])
        for bad in (0, 6, True, '4', float('nan')):
            with self.assertRaises(ValueError):
                rank(['pass'], bad, 3)

    def test_event_bounds_filters_and_unknown_after(self):
        trace = sample()
        trace['journal'] = [event('1', 'memory.recorded', payload={'kind': 'x'}), event('2', 'memory.recorded')]
        self.assertTrue(evaluate({'kind': 'event', 'type': 'memory.recorded', 'min': 2}, trace, {})[0])
        self.assertFalse(evaluate({'kind': 'event', 'type': 'memory.recorded', 'max': 1}, trace, {})[0])
        self.assertTrue(evaluate({'kind': 'event', 'type': 'memory.recorded', 'where': {'kind': '*'}, 'min': 1}, trace, {})[0])
        self.assertTrue(evaluate({'kind': 'event', 'type': 'memory.recorded', 'where': {'kind': 'x'}, 'min': 1}, trace, {})[0])
        self.assertTrue(evaluate({'kind': 'absent', 'type': 'other'}, trace, {})[0])
        self.assertFalse(evaluate({'kind': 'absent', 'type': 'memory.recorded'}, trace, {})[0])
        self.assertIsNone(evaluate({'kind': 'absent', 'type': 'other', 'after': 'choose'}, trace, {})[0])

    def test_state_and_snapshot_never_use_missing_as_success(self):
        trace = sample()
        self.assertEqual(lookup(trace['final_state'], 'intentions.x.status'), (True, 'active'))
        for a, want in [({'path': 'absent', 'op': 'absent'}, True),
                        ({'path': 'absent', 'op': 'eq', 'value': None}, False),
                        ({'path': 'intentions.x.status', 'op': 'in', 'value': ['active']}, True),
                        ({'path': 'intentions.x.status', 'op': 'contains', 'value': 'act'}, True),
                        ({'path': 'v', 'op': 'eq', 'value': 1, 'after': 'choose'}, True),
                        ({'path': 'v', 'op': 'eq', 'value': 1, 'after': 'missing'}, None),
                        ({'path': 'intentions', 'op': 'unknown'}, None)]:
            self.assertIs(evaluate({'kind': 'state', **a}, trace, {})[0], want)

    def test_text_scopes_do_not_include_unread_archive(self):
        trace = sample()
        trace['journal'] = [event('secret', 'input.arrived', payload={'text': 'UNREAD'})]
        for scope in ('model_requests', 'memory_retrieval', 'state', 'outbound_messages'):
            self.assertTrue(evaluate({'kind': 'text', 'scope': scope, 'marker': 'UNREAD', 'presence': 'absent'}, trace, {})[0])
        trace['decision_trace'][0]['proposals'][0]['response']['proposals'][0] = {'kind': 'message', 'body': {'text': 'VISIBLE'}}
        a = {'kind': 'text', 'scope': 'outbound_messages', 'marker': 'VISIBLE', 'presence': 'present'}
        self.assertFalse(evaluate(a, trace, {})[0])
        trace['decision_trace'][0]['proposals'][0]['response']['proposals'][0]['proposal_id'] = 'sent1'
        trace['journal'].append(event('sent', 'message.out_sent', payload={'proposal_id': 'sent1'}))
        self.assertTrue(evaluate(a, trace, {})[0])
        self.assertIsNone(evaluate({'kind': 'text', 'scope': 'unknown', 'marker': 'x', 'presence': 'present'}, trace, {})[0])

    def test_causal_chain_is_transitive_and_handles_cycles_missing_refs(self):
        trace = sample()
        trace['journal'] = [event('1', 'memory.retrieved'), event('2', 'middle', ['1']),
                            event('3', 'proposal.accepted', ['2', 'absent']), event('4', 'cycle', ['4'])]
        check = {'kind': 'chain', 'effect': 'proposal.accepted', 'caused_by': 'memory.retrieved'}
        self.assertTrue(evaluate(check, trace, {})[0])
        self.assertFalse(evaluate({**check, 'caused_by': 'missing'}, trace, {})[0])
        self.assertFalse(evaluate({**check, 'effect': 'cycle'}, trace, {})[0])

    def test_comparison_uses_proposal_bodies_not_technical_ids(self):
        a, b = sample(), sample()
        b['decision_trace'][0]['proposals'][0]['response']['proposals'][0]['proposal_id'] = 'different-id'
        b['journal'][0]['payload']['proposal_id'] = 'different-id'
        check = {'kind': 'compare', 'runs': ['a', 'b'], 'subject': 'journal:proposal.accepted', 'relation': 'same'}
        self.assertTrue(evaluate(check, a, {'a': a, 'b': b})[0])
        b['decision_trace'][0]['proposals'][0]['response']['proposals'][0]['body']['reason'] = 'другое решение'
        self.assertTrue(evaluate({**check, 'relation': 'differs'}, a, {'a': a, 'b': b})[0])
        self.assertIsNone(evaluate(check, a, {'a': a})[0])
        self.assertIsNone(evaluate({**check, 'subject': 'x:y'}, a, {'a': a, 'b': b})[0])
        self.assertIsNone(evaluate({**check, 'relation': 'unknown'}, a, {'a': a, 'b': b})[0])
        self.assertEqual(subject(a, 'state:intentions.x.status'), (True, 'active'))
        self.assertIsNone(evaluate({**check, 'subject': 'state:missing'}, a, {'a': a, 'b': b})[0])
        self.assertIsNone(evaluate({'kind': 'unknown'}, a, {})[0])

    def test_accepted_behavior_ignores_reference_ids_but_preserves_human_text(self):
        a, b = sample(), sample()
        for trace, ref in ((a, 'evt_00000001'), (b, 'evt_00000002')):
            p = trace['decision_trace'][0]['proposals'][0]['response']['proposals'][0]
            p.update(kind='attend', body={'focus': 'event', 'ref': ref, 'params': {'link': ref, 'refs': [ref]}})
        self.assertEqual(subject(a, 'journal:proposal.accepted'), subject(b, 'journal:proposal.accepted'))
        b['decision_trace'][0]['proposals'][0]['response']['proposals'][0]['body']['focus'] = 'activity'
        self.assertNotEqual(subject(a, 'journal:proposal.accepted'), subject(b, 'journal:proposal.accepted'))
        for key, left, right in (('ref', 'chapter_one', 'chapter_two'),
                                 ('capability_id', 'cap_A', 'cap_B')):
            for trace, value in ((a, left), (b, right)):
                trace['decision_trace'][0]['proposals'][0]['response']['proposals'][0].update(
                    kind='action' if key == 'capability_id' else 'attend', body={key: value})
            self.assertNotEqual(subject(a, 'journal:proposal.accepted'), subject(b, 'journal:proposal.accepted'))
        b['journal'] = []
        self.assertEqual(subject(b, 'journal:proposal.accepted'), [])
        with self.assertRaisesRegex(ValueError, 'max_format_retries'):
            BoundedProvider(SimpleNamespace(max_format_retries=1))

    def test_assessment_retains_unknown_fail_and_source_links(self):
        scenario = Scenario.from_dict({'format': 1, 'id': 'test', 'group': 'experiments',
            'step': [{'id': 'start', 'kind': 'session', 'action': 'start'}],
            'assert': [{'id': 'x', 'kind': 'absent', 'type': 'missing', 'class': 'technical'},
                       {'id': 'y', 'kind': 'unknown', 'class': 'causal'},
                       {'id': 'z', 'run': 'other', 'kind': 'unknown', 'class': 'technical'}]})
        rows = assess(scenario, [RunResult(self.root / 'trace', sample())])
        self.assertEqual([r['status'] for r in rows], ['pass', 'unverified'])
        self.assertTrue(all(r['trace'].endswith('trace.json') for r in rows))

    def test_blind_reproducible_separate_key_and_no_variant_markers(self):
        results = [RunResult(self.root / str(n), sample('Текст номер ' + str(n))) for n in range(20)]
        first = build_blind(results, self.root / 'public', self.root / 'private/key.json')
        second = build_blind(results, self.root / 'public2', self.root / 'private/key2.json')
        self.assertEqual(first, second)
        self.assertNotEqual([r['trace'] for r in first], [str(r.path / 'trace.json') for r in results])
        self.assertFalse((self.root / 'public/key.json').exists())
        for item in first:
            raw = (self.root / 'public' / (item['id'] + '.md')).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item['sha256'])
            self.assertIsNone(MARKERS.search(raw.decode()))
        ratings = list(csv.DictReader(io.StringIO((self.root / 'public/ratings.csv').read_text())))
        self.assertTrue(all(r['recognizability'] == '' for r in ratings))
        with self.assertRaises(FileExistsError):
            build_blind(results, self.root / 'new-public', self.root / 'private/key.json')
        self.assertFalse((self.root / 'new-public').exists())
        with self.assertRaises(FileExistsError):
            build_blind(results, self.root / 'public', self.root / 'key3.json')
        with self.assertRaises(ValueError):
            build_blind(results, self.root / 'leak', self.root / 'leak/key.json')

    def test_blind_omits_markers_without_rewriting_and_handles_supported_bodies(self):
        trace = sample('Правила C: тайный вариант')
        build_blind([RunResult(self.root, trace)], self.root / 'empty', self.root / 'key.json')
        key = json.loads((self.root / 'key.json').read_text())
        self.assertEqual(key['items'], [])
        self.assertEqual(len(key['omitted']), 1)
        p = trace['decision_trace'][0]['proposals'][0]['response']['proposals'][0]
        p.update(kind='action', body={'params': {'text': 'Заметка о звёздах'}})
        self.assertIn('Заметка о звёздах', fragment(trace))
        p.update(kind='message', body={'text': 'Привет'})
        self.assertIn('Привет', fragment(trace))
        p.update(kind='attend', body={})
        self.assertEqual(fragment(trace), '')
        p.update(kind='no_action', body={'reason': 7})
        self.assertEqual(fragment(trace), '')

    def test_bounded_provider_counts_failures_and_stops_at_ten_or_time(self):
        p = BoundedProvider(ScriptedProvider([Reply(error=ProviderUnavailable('hidden'))] * 10))
        with self.assertRaises(ProviderUnavailable):
            p.propose({})
        with self.assertRaises(SessionLimitReached):
            p.propose({})
        p = BoundedProvider(ScriptedProvider([Reply()] * 10))
        request = {'request_id': 'r', 'epoch': 1, 'state_version': 0}
        for _ in range(10):
            p.propose(request)
        with self.assertRaises(SessionLimitReached):
            p.propose(request)
        p = BoundedProvider(ScriptedProvider())
        with patch('iam.experiments.report.suite.time.monotonic', return_value=p.started + 1801):
            with self.assertRaises(SessionLimitReached):
                p.propose({})

    def test_format_error_allows_one_core_repair_without_technical_pause(self):
        p = BoundedProvider(ScriptedProvider([Reply(error=ProviderFormatError('format')), Reply()]))
        req = {'request_id': 'r', 'epoch': 1, 'state_version': 0}
        with self.assertRaises(ProviderFormatError):
            p.propose(req)
        self.assertEqual(p.propose(req)['status'], 'ok')
        self.assertEqual(p.calls, 2)

    def test_full_matrix_same_source_bills_and_isolated_valid_stores(self):
        summary = run_suite(ROOT, self.root / 'suite')
        self.assertEqual((summary['matrix_cells'], summary['executed'], summary['subruns']), (48, 48, 72))
        matrix = list(csv.DictReader(io.StringIO((self.root / 'suite/matrix.csv').read_text())))
        for row in matrix:
            original = ROOT / 'scenarios/experiments' / (row['scenario'] + '.toml')
            self.assertEqual(row['source_sha256'], hashlib.sha256(original.read_bytes()).hexdigest())
        for run in summary['runs']:
            raw = Path(run['trace']).read_bytes()
            self.assertEqual(run['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(run['integrity'], 'pass')
            self.assertTrue(all(x in ('done', 'rejected') for x in run['outcomes']))
        self.assertTrue(any(r['memory_off'] for r in summary['runs']))
        self.assertTrue(any(r['motivation_off'] for r in summary['runs']))
        self.assertTrue(all(r['calls_reported'] == 0 for r in summary['runs'] if r['cell'].startswith('C')))
        self.assertTrue(any(r['technical_fail'] for r in summary['runs']))
        with self.assertRaises(FileExistsError):
            run_suite(ROOT, self.root / 'suite')

    def test_formal_mode_accounts_for_every_unperformed_cell(self):
        summary = run_suite(ROOT, self.root / 'formal', mode='formal')
        self.assertEqual(summary['executed'], 16)
        rows = list(csv.DictReader(io.StringIO((self.root / 'formal/matrix.csv').read_text())))
        self.assertEqual(sum(r['status'] == 'not_run' and bool(r['reason']) for r in rows), 32)
        with self.assertRaises(ValueError):
            run_suite(ROOT, self.root / 'bad', mode='live')
        with self.assertRaises(ValueError):
            run_suite(ROOT, self.root / 'bad2', mode='wrong')
        with patch('iam.experiments.report.suite.Path.glob', return_value=[]):
            with self.assertRaises(ValueError):
                run_suite(ROOT, self.root / 'bad3')

    def test_live_error_pauses_language_variants_without_stub_fallback(self):
        summary = run_suite(ROOT, self.root / 'live', mode='live',
            provider_factory=lambda _: ScriptedProvider([Reply(error=ProviderUnavailable('RAW-SECRET'))] * 4))
        self.assertEqual(summary['executed'], 17)
        self.assertNotIn('RAW-SECRET', json.dumps(summary))
        self.assertEqual(summary['runs'][0]['outcomes'], ['no_decision', 'no_decision'])

    def test_variant_gate_requires_complete_live_matrix_integrity_and_decisions(self):
        cells = [{'variant': 'A', 'status': 'executed'} for _ in range(16)]
        runs = [{'cell': 'A-case', 'integrity': 'pass', 'status': 'completed', 'outcomes': ['done']}]
        check = lambda cs=cells, rs=runs, mode='live': rank(eligibility('A', cs, rs, ['pass'], mode), 5, 5)['eligible']
        self.assertTrue(check())
        self.assertFalse(check(cs=cells[:15]))
        self.assertFalse(check(cs=[{**c, 'status': 'not_run'} for c in cells]))
        self.assertFalse(check(mode='stub'))
        self.assertFalse(check(rs=[]))
        for change in ({'integrity': 'fail'}, {'status': 'decision_limit'}, {'outcomes': ['no_decision']}):
            self.assertFalse(check(rs=[{**runs[0], **change}]))

    def test_comparison_is_evaluated_once_and_links_every_participant(self):
        scenario = Scenario.from_dict({'format': 1, 'id': 'test', 'group': 'experiments',
            'step': [{'id': 'start', 'kind': 'session', 'action': 'start'}],
            'assert': [{'id': 'compare', 'kind': 'compare', 'class': 'causal', 'runs': ['one', 'two'],
                        'subject': 'journal:proposal.accepted', 'relation': 'same'}]})
        results = [RunResult(self.root / name, {**sample(), 'run': name}) for name in ('one', 'two', 'unrelated')]
        rows = assess(scenario, results)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['run'], 'one;two')
        self.assertIn('/one/trace.json;', rows[0]['trace'])
        self.assertTrue(rows[0]['trace'].endswith('/two/trace.json'))
        self.assertEqual(rows[0]['status'], 'pass')

    def test_cli_formal_and_explicit_live_config_without_network_in_tests(self):
        with patch('iam.experiments.report.__main__.run_suite', return_value={'matrix_cells': 48, 'executed': 0, 'subruns': 0}) as run:
            self.assertEqual(main(['--output', str(self.root / 'cli'), '--mode', 'formal']), 0)
            self.assertIsNone(run.call_args.kwargs['provider_factory'])
            with patch('iam.experiments.report.__main__.CodexSandboxConfig'):
                self.assertEqual(main(['--output', str(self.root / 'cli2'), '--mode', 'live', '--auth-file', '/external/auth']), 0)
                with patch('iam.experiments.report.__main__.CodexProvider') as provider:
                    run.call_args.kwargs['provider_factory'](None)
                    self.assertEqual(provider.call_args.kwargs['max_format_retries'], 0)
        with self.assertRaises(SystemExit):
            main(['--output', str(self.root / 'cli3'), '--mode', 'live'])
