"""Механическая проверка продолжения; подготовленные цели не являются живым успехом."""
import json
import unittest
from unittest.mock import patch
from iam.providers import CodexProvider,CodexSandboxConfig
from iam.providers.codex import fake
from iam.stubs import ScriptedProvider,Reply,rest
from integration import continue_goal as continuation,intention_probe as probe,test_intention_probe as fixtures


class GoalContinuationTest(unittest.TestCase):
    def setUp(self):
        fixtures.IntentionProbeTest.setUp(self)
        def factory(name,transport):
            return ScriptedProvider([Reply((fixtures.IntentionProbeTest.goal(self,name[0]),)),
                                     Reply((fixtures.IntentionProbeTest.goal(self,name[0]),)),Reply((rest(),))])
        result=probe.run_probe(self.source,self.output,protocol='options',provider_factory=factory)
        self.assertEqual(result['status'],'completed')
        self.accepted=self.output/'cells/A-options_visible'
        self.continued=self.root/'continued'

    def test_reopened_state_retains_actual_goal_without_injected_instructions(self):
        source_sha=probe.series.sha(self.accepted/'persona.db')
        result=continuation.run_continuation(self.accepted,self.continued)
        self.assertEqual(probe.series.sha(self.accepted/'persona.db'),source_sha)
        self.assertEqual((result['status'],result['decisions'],result['model_calls']),('completed',2,0))
        manifest=json.loads((self.continued/'manifest.json').read_text())
        self.assertEqual(manifest['injected_inputs'],0)
        for number in (1,2):
            entry=json.loads((self.continued/f'sessions/{number}/result.json').read_text())
            self.assertEqual(entry['recovered_goal'],manifest['original_goal'])
            self.assertEqual(entry['goal'],manifest['original_goal'])
            self.assertEqual(entry['report']['outcome'],'done')
            self.assertEqual(len(entry['traces']),1)
            request=entry['traces'][0]['request']
            self.assertEqual(request['purpose'],'choose_action')
            self.assertIn(manifest['goal_id'],json.dumps(request['context']))
            self.assertNotIn('EXPERIMENTAL: обычное продолжение',json.dumps(request['context']))

    def test_physical_answers_are_charged_and_preserved_across_restarts(self):
        def factory(number,transport):return CodexProvider(transport,CodexSandboxConfig(),max_format_retries=0)
        run=fake.reply_text(json.dumps({'proposals':[{'kind':'no_action','rationale':'Rest','cites':[],
                                                   'body_json':'{"reason":"Rest"}'}],
                                        'attention':None,'explanation':'Rest'}))
        with patch.object(probe,'subprocess_runner',return_value=run) as runner:
            result=continuation.run_continuation(self.accepted,self.continued,mode='live',
                       auth_file=str(self.root/'private.json'),provider_factory=factory)
        self.assertEqual((result['status'],result['decisions'],result['model_calls'],runner.call_count),('completed',2,2,2))
        self.assertEqual(len(list(self.continued.glob('sessions/*/physical/*.json'))),2)
        for number in (1,2):
            entry=json.loads((self.continued/f'sessions/{number}/result.json').read_text())
            self.assertEqual(entry['usage']['calls'],1)

    def test_changed_frozen_sources_stop_before_first_decision(self):
        original=probe.frozen_sources()
        with patch.object(probe,'frozen_sources',side_effect=[original,{}]):
            result=continuation.run_continuation(self.accepted,self.continued)
        self.assertEqual((result['status'],result['decisions'],result['model_calls']),('stopped',0,0))

    def test_global_and_session_physical_caps_stop_before_seventh_call(self):
        class OverCalls:
            name='test'
            def __init__(self,transport):self.transport=transport
            def propose(self,request):
                for _ in range(7):self.transport(('fake',),'PRIVATE_INPUT',1)
                raise AssertionError('Must stop before seventh transport')
        with patch.object(probe,'subprocess_runner',return_value=fake.reply_text('{"proposals":[],"attention":null,"explanation":""}')) as runner:
            result=continuation.run_continuation(self.accepted,self.continued,mode='live',
                       auth_file=str(self.root/'private.json'),provider_factory=lambda n,t:OverCalls(t))
        self.assertEqual((result['status'],result['decisions'],result['model_calls'],runner.call_count),('stopped',1,6,6))
        for artifact in self.continued.rglob('*.json'):self.assertNotIn('PRIVATE_INPUT',artifact.read_text())

    def test_mismatching_or_absent_original_goal_is_rejected_before_output(self):
        path=self.accepted/'result.json'
        original=json.loads(path.read_text())
        for mode in ('mismatch','absent'):
            data=json.loads(json.dumps(original))
            if mode=='mismatch':next(iter(data['new_intentions'].values()))['criterion']='Invented criterion'
            else:data['new_intentions']={}
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):continuation.run_continuation(self.accepted,self.continued)
            self.assertFalse(self.continued.exists())
