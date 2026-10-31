"""Допуск полных данных не является требованием безошибочности личности."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest

from iam.experiments.report.development import run_development, development_packet, assess_development
from iam.experiments.report.review import admission, full_biography, proposal_text, review_packet, CERTIFICATE_CHECKS
from iam.experiments.results import RunResult

ROOT = Path(__file__).resolve().parents[2]


def certificate(result):
    return {'trace_sha256': hashlib.sha256(result.trace_bytes).hexdigest(), **dict.fromkeys(CERTIFICATE_CHECKS, True)}


class ReviewCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        summary = run_development(ROOT, cls.root / 'formal', mode='formal', repeats=1)
        cls.results = [RunResult(Path(r['trace']).parent, json.loads(Path(r['trace']).read_text()))
                       for r in summary['runs'] if r['status'] == 'executed']

    def setUp(self):
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        self.path = Path(self.output.name)

    def manifest(self):
        names = ['src/iam/experiments/development.py', 'src/iam/experiments/report/development.py',
                 'src/iam/experiments/report/__main__.py', 'src/iam/providers/codex/command.py',
                 'scenarios/personas/curious.toml']
        return {'runtime': 'development-v6', 'model': 'gpt-6-luna', 'variants': ['A','B','C'], 'repeats': 2,
                'decisions_per_session': 3, 'sessions': 6, 'max_calls_per_session': 10, 'max_session_seconds': 1800,
                'execution_sources': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}}

    def isolation(self):
        names = ['schema','network_internal','runtime','non_root','read_only_root','no_capabilities','no_host_mounts',
                 'no_socket','limits','ephemeral_auth','fresh_home','net_allowed','net_denied','net_direct_denied']
        path = self.path/'isolation.json'
        path.write_text(json.dumps({'model':'gpt-6-luna','isolation_passed':True,'checks':dict.fromkeys(names,'PASS')}))
        return path

    def test_safe_full_control_is_reviewable_even_when_old_personality_filter_fails(self):
        result = self.results[0]
        self.assertFalse(assess_development(result.trace)['structural_admission'])
        self.assertTrue(admission(result, certificate(result))['eligible'])
        altered = copy.deepcopy(result.trace)
        altered['decision_trace'][0]['report']['outcome'] = 'rejected'
        altered['decision_trace'][0]['report']['stages'].append({'stage': 'appraisal', 'status': 'failed'})
        changed = RunResult(result.path, altered)
        self.assertTrue(admission(changed, certificate(changed))['eligible'])
        self.assertIsNone(admission(changed, certificate(changed))['personality_score'])

    def test_certificate_hash_and_every_technical_check_are_required(self):
        result = self.results[0]
        self.assertFalse(admission(result, {})['eligible'])
        for name in CERTIFICATE_CHECKS:
            for value in (False, None, 1):
                cert = {**certificate(result), name: value}
                self.assertFalse(admission(result, cert)['eligible'], (name, value))
        cert = {**certificate(result), 'trace_sha256': 'wrong'}
        self.assertFalse(admission(result, cert)['eligible'])

    def test_missing_choice_session_or_restore_is_missing_data_not_personality_failure(self):
        result = self.results[0]
        changes = [lambda t: t.update(status='incomplete'),
                   lambda t: t['decision_trace'].pop(),
                   lambda t: t['decision_trace'][0].update(session=99),
                   lambda t: t['sessions'].pop(),
                   lambda t: t['sessions'][0].update(session=99),
                   lambda t: t['sessions'][0].update(restored=False)]
        for change in changes:
            trace = copy.deepcopy(result.trace); change(trace)
            changed = RunResult(result.path, trace)
            self.assertFalse(admission(changed, certificate(changed))['eligible'])
        self.assertFalse(admission(RunResult(self.path, {}), {})['eligible'])

    def test_full_real_trace_keeps_every_choice_and_every_saved_version(self):
        trace = self.results[0].trace
        text = full_biography(trace)
        self.assertEqual(text.count('### Момент выбора '), 18)
        self.assertEqual(text.count('## Сеанс '), 6)
        for project in trace['final_state']['world_time']['world']['development']['projects'].values():
            for version in project['versions']:
                self.assertIn(version['text'], text)
        self.assertNotIn(trace['architecture'], text)
        self.assertIn('сохранён будущий замысел', text)
        self.assertIn('Прочитано от человека', text)

    def detailed_trace(self):
        proposals = {}
        events = []
        def event(kind, payload):
            ident = 'evt_' + str(len(events)).zfill(8)
            events.append({'event_id': ident, 'type': kind, 'payload': payload})
            return ident
        def proposal(kind, body):
            ident = 'prop_' + str(len(proposals)).zfill(8)
            proposals[ident] = {'proposal_id': ident, 'kind': kind, 'body': body}
            return ident
        event('session.started', {})
        event('input.arrived', {'input_id': 'one', 'text': 'READ-ME'})
        event('input.arrived', {'input_id': 'two', 'text': 'UNREAD-PRIVATE'})
        trigger = event('time.moment_due', {})
        event('input.read', {'input_id': 'one'})
        sent = proposal('message', {'text': 'cap_обычная ошибка слов — оставить дословно'})
        event('message.out_sent', {'proposal_id': sent})
        write = proposal('action', {'params': {'operation': 'write_project', 'title': 'Текст', 'text': 'RAW\nполная версия'}})
        event('action.confirmed', {'proposal_id': write})
        for operation in ('read_material', 'open_project', 'rest', 'read_message'):
            ident = proposal('action', {'params': {'operation': operation}})
            event('action.confirmed', {'proposal_id': ident, 'summary': 'Исходный материал'})
        for kind, ident in (('action.failed', write), ('action.unknown_outcome', write), ('message.out_unknown', sent)):
            event(kind, {'proposal_id': ident})
        empty = proposal('action', {'params': {'operation': 'rest'}})
        event('action.failed', {'proposal_id': empty})
        event('memory.recorded', {'memory_id': 'own'})
        event('memory.recorded', {'memory_id': 'observation'})
        for body in ({'op': 'create', 'id': 'goal'}, {'op': 'transition', 'id': 'goal', 'to_status': 'done'},
                     {'op': 'transition', 'id': 'goal', 'to_status': 'deferred'}):
            ident = proposal('intention', body); event('proposal.accepted', {'proposal_id': ident})
        ident = proposal('no_action', {'reason': 'Не хочу сейчас действовать.'})
        event('proposal.accepted', {'proposal_id': ident})
        ident = proposal('memory_write', {'draft': {'content': {'text': 'Ложное предположение — не сохранялось.'}}})
        event('proposal.rejected', {'proposal_id': ident})
        ident = proposal('memory_write', {'draft': []})
        event('proposal.rejected', {'proposal_id': ident})
        end = event('proposal.rejected', {'proposal_id': 'missing'})
        event('session.stopped', {})
        return {'journal': events, 'memory_records': [
                    {'memory_id': 'own', 'origin': 'own_hypothesis', 'content': {'text': 'Мне кажется, что я ошибаюсь.'}},
                    {'memory_id': 'observation', 'origin': 'observation', 'content': {'text': 'service'}}],
                'final_state': {'intentions': {'goal': {'text': 'Свой замысел'}}},
                'decision_trace': [{'report': {'trigger': trigger, 'outcome': 'rejected'}, 'grounds': [{'event': end}],
                    'requests': [{'retry_of': 'previous'}],
                    'proposals': [{'response': {'proposals': list(proposals.values())}}]}]}

    def test_full_biography_preserves_errors_unconfirmed_text_and_unread_boundary(self):
        text = full_biography(self.detailed_trace())
        self.assertNotIn('UNREAD-PRIVATE', text)
        self.assertIn('READ-ME', text)
        self.assertIn('RAW\nполная версия', text)
        self.assertIn('cap_обычная ошибка слов — оставить дословно', text)
        self.assertIn('Ложное предположение — не сохранялось.', text)
        self.assertIn('Мне кажется, что я ошибаюсь.', text)
        self.assertIn('результат не подтверждён', text)
        self.assertIn('сама отметила выполненным', text)
        self.assertIn('без принятого результата', text)
        self.assertIn('потребовалось исправление формы', text)

    def test_malformed_draft_text_is_not_fabricated_by_renderer(self):
        for body in ({'draft': []}, {'draft': {'content': []}}, {'draft': {'content': {'text': 1}}}):
            self.assertEqual(proposal_text({'kind': 'memory_write', 'body': body}), '')
        for body in ({'params': []}, {'params': {'text': []}}):
            self.assertEqual(proposal_text({'kind': 'action', 'body': body}), '')
        self.assertEqual(proposal_text({'kind': 'message', 'body': {'text': 'copy'}}), 'copy')

    def test_packet_includes_all_certified_histories_reproduces_and_keeps_scores_blank(self):
        certs = {str(result.path): certificate(result) for result in self.results}
        first = development_packet(self.results, self.path/'one', self.path/'private/one.json',
                                   policy='fallible-v1', technical_reviews=certs)
        second = review_packet(self.results, self.path/'two', self.path/'private/two.json', technical_reviews=certs)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        self.assertEqual((self.path/'private/one.json').read_bytes(), (self.path/'private/two.json').read_bytes())
        for file in (self.path/'one').iterdir():
            self.assertEqual(file.read_bytes(), (self.path/'two'/file.name).read_bytes())
        self.assertIn('D001,,,,,', (self.path/'one/ratings.csv').read_text())
        self.assertIn('[D002](D002.md)', (self.path/'one/INDEX.md').read_text())
        self.assertFalse(list((self.path/'one').rglob('*.json')))
        for row in first:
            raw = (self.path/'one'/(row['id']+'.md')).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row['sha256'])
        with self.assertRaises(FileExistsError):
            review_packet([], self.path/'three', self.path/'private/one.json', technical_reviews={})
        with self.assertRaises(FileExistsError):
            review_packet([], self.path/'one', self.path/'private/new.json', technical_reviews={})
        with self.assertRaises(ValueError):
            review_packet([], self.path/'inside', self.path/'inside/key.json', technical_reviews={})
        with self.assertRaises(ValueError):
            development_packet([], self.path/'bad', self.path/'bad-key', policy='unknown')

    def test_unverified_history_is_logged_as_omitted_and_never_receives_a_grade(self):
        mapping = development_packet(self.results, self.path/'empty', self.path/'key.json', policy='fallible-v1')
        self.assertEqual(mapping, [])
        key = json.loads((self.path/'key.json').read_text())
        self.assertEqual(len(key['omitted']), 2)
        self.assertEqual((self.path/'empty/ratings.csv').read_text().count('\n'), 1)

    def test_preflight_requires_equal_complete_protocol_and_actual_source_hashes(self):
        verify = runpy.run_path(str(ROOT/'scripts/experiments/verify_development.py'))['review_preflight']
        manifest = self.manifest();(self.path/'series-manifest.json').write_text(json.dumps(manifest))
        rows = [{'cell':f'{v}-{h}-{n}','variant':v,'history':h,'repeat':n}
                for v in ('A','B','C') for h in ('supportive','distant') for n in (1,2)]
        summary = {'mode':'live','variants':['A','B','C'],'repeats':2,'executed':12,'runs':rows}
        isolation = self.isolation()
        self.assertTrue(verify(self.path,summary,isolation))
        self.assertFalse(verify(self.path,{**summary,'mode':'stub'},isolation))
        altered = copy.deepcopy(manifest);altered['execution_sources']['src/iam/experiments/development.py']='bad'
        (self.path/'series-manifest.json').write_text(json.dumps(altered))
        with self.assertRaises(AssertionError):verify(self.path,summary,isolation)
        (self.path/'series-manifest.json').write_text(json.dumps(manifest))
        broken=copy.deepcopy(summary);broken['runs'][0]=broken['runs'][1]
        with self.assertRaises(AssertionError):verify(self.path,broken,isolation)
        raw=json.loads(isolation.read_text());raw['checks']['no_socket']='FAIL';isolation.write_text(json.dumps(raw))
        with self.assertRaises(AssertionError):verify(self.path,summary,isolation)

    def test_executor_outage_is_distinct_from_rejected_guess_or_format_error(self):
        available=runpy.run_path(str(ROOT/'scripts/experiments/verify_development.py'))['executor_available']
        trace=copy.deepcopy(self.results[0].trace)
        self.assertTrue(available(trace))
        trace['decision_trace'][0]['report']['stages'].append({'stage':'proposal','detail':{'error':'ProviderFormatError'}})
        self.assertTrue(available(trace))
        trace['decision_trace'][0]['report']['stages'].append({'stage':'appraisal','detail':{'phase':'development_appraisal',
            'report':{'stages':[{'stage':'proposal','detail':{'error':'ProviderAuthError'}}]}}})
        self.assertFalse(available(trace))

    def test_verifier_keeps_legacy_compatibility_and_cannot_publish_stub_as_live(self):
        check=runpy.run_path(str(ROOT/'scripts/experiments/verify_development.py'))['check']
        source=self.path/'raw';run_development(ROOT,source,mode='stub',repeats=2)
        (source/'series-manifest.json').write_text(json.dumps(self.manifest()))
        # Это явно локальная заглушка. Из неё не должно получиться пакета для человека.
        outcome=check(source,self.path/'publish',policy='fallible-v1',review_key=self.path/'private/review.json',
                      isolation_report=self.isolation())
        self.assertEqual(outcome['human_packet'],0)
        self.assertEqual(outcome['databases_checked'],60)
        self.assertTrue((self.path/'private/review.json').exists())
        self.assertFalse(list((self.path/'publish/blind').glob('D*.md')))
        for kwargs in ({'policy':'bad'}, {'policy':'fallible-v1'},
                       {'policy':'fallible-v1','review_key':ROOT/'key.json','isolation_report':self.isolation()}):
            with self.assertRaises(ValueError):check(source,self.path/'invalid',**kwargs)


if __name__ == '__main__':
    unittest.main()
