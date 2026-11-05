"""Проводка отдельной оценки, равный контроль, реальный учёт и конечный стенд."""
import json
import unittest
from unittest.mock import patch

from iam.providers import CodexProvider, CodexSandboxConfig
from iam.providers.codex import fake
from iam.state import StateController
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, rest
from iam.world import WRITE
from iam.world.prospective import ProspectiveCycle
from integration import intention_probe as probe
from integration import test_intention_probe as fixtures
from integration.test_persistent_core import wire, tick


class ProspectiveProbeTest(unittest.TestCase):
    setUp = fixtures.IntentionProbeTest.setUp
    goal = fixtures.IntentionProbeTest.goal

    def test_same_past_control_and_new_phase_are_distinct_and_goal_reaches_following_action(self):
        def factory(name, transport):
            if name.endswith('baseline'): return ScriptedProvider([Reply((rest(),))])
            first = (self.goal(name[0]),) if name.endswith('prospective') else ()
            action = act(WRITE, {'project_id': 'probe', 'title': 'Подготовленная проверка', 'text': 'Свет'})
            return ScriptedProvider([Reply(first), Reply((action,))])
        result = probe.run_probe(self.source, self.output, protocol='prospective', provider_factory=factory)
        self.assertEqual((result['status'], result['decisions'], result['model_calls']), ('completed', 6, 0))
        self.assertEqual([c['new_intentions'] for c in result['cells']], [0, 0, 1, 0, 0, 1])
        for story in probe.STORIES:
            self.assertEqual(len({c['before_digest'] for c in result['cells'] if c['name'].startswith(story)}), 1)
            control = json.loads((self.output / f'cells/{story}-assessment_only/result.json').read_text())
            new = json.loads((self.output / f'cells/{story}-prospective/result.json').read_text())
            self.assertEqual([q['request']['purpose'] for q in new['traces']], ['appraise', 'choose_action'])
            self.assertEqual(len(control['traces']), len(new['traces']))
            self.assertIn('probe', json.dumps(new['traces'][1]['request']['context']))
            self.assertNotIn('intention', new['traces'][1]['request']['allowed_proposals'])
        manifest = json.loads((self.output/'manifest.json').read_text())
        self.assertEqual((manifest['max_model_calls'], manifest['cell_model_calls']), (24, 4))
        self.assertNotEqual(manifest['appraisal_instructions'], manifest['control_instructions'])

    def test_each_real_transport_including_appraisal_is_saved_and_charged(self):
        def factory(name, transport):
            return CodexProvider(transport, CodexSandboxConfig(), max_format_retries=0)
        def physical(argv, stdin, timeout, cleanup):
            from iam.providers.codex.command import SYSTEM_PROMPT
            q = json.loads(json.loads(stdin)['prompt'][len(SYSTEM_PROMPT+'\n\n'):])
            return fake.reply([]) if q['purpose'] == 'appraise' else fake.reply([
                {'kind':'no_action','rationale':'Механический отдых','cites':[],'body':{'reason':'Пауза'}}])
        with patch.object(probe, 'subprocess_runner', side_effect=physical) as runner:
            result = probe.run_probe(self.source, self.output, protocol='prospective', mode='live',
                                     auth_file=str(self.root/'private.json'), provider_factory=factory)
        self.assertEqual((result['status'], result['model_calls'], runner.call_count), ('completed', 10, 10))
        self.assertEqual(len(list(self.output.rglob('physical/*.json'))), 10)
        for name in ('A-assessment_only', 'A-prospective', 'E-assessment_only', 'E-prospective'):
            with Store.open(self.output/f'cells/{name}/persona.db') as store:
                from iam.session import SessionRunner
                self.assertEqual(SessionRunner(StateController(store)).usage()['calls'], 2)

    def test_one_call_session_cannot_spend_an_extra_call_after_appraisal(self):
        path = self.source/'stories/A/persona.db'
        with Store.open(path) as store:
            ctl = StateController(store)
            provider = ScriptedProvider([Reply((self.goal('A'),)), Reply((rest(),))])
            session, cycle, _ = wire(ctl, provider=provider, cycle_type=ProspectiveCycle)
            session.command('start', 'Проверить реальный общий бюджет', max_calls=1)
            result = tick(cycle)
            self.assertEqual(result.outcome, 'no_decision')
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(ctl.session_state, 'paused')
            self.assertEqual(session.usage()['calls'], 1)
            self.assertIn('probe', ctl.state['intentions'])

    def test_invalid_appraisal_stops_without_repair_and_preserves_its_raw(self):
        def factory(name, transport):
            return CodexProvider(transport, CodexSandboxConfig(), max_format_retries=0)
        with patch.object(probe, 'subprocess_runner', side_effect=[fake.reply([
                {'kind':'no_action','rationale':'Механическая пауза','cites':[],'body':{'reason':'Пауза'}}]),
                fake.reply_text('{bad')]) as runner:
            result = probe.run_probe(self.source, self.output, protocol='prospective', mode='live',
                                     auth_file=str(self.root/'private.json'), provider_factory=factory)
        self.assertEqual((result['status'], result['model_calls'], runner.call_count), ('stopped', 2, 2))
        raw = json.loads((self.output/'cells/A-assessment_only/physical/002.json').read_text())
        self.assertEqual(raw['messages'], ['{bad'])
