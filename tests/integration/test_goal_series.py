"""Контракт стенда; подготовленные ответы не являются свидетельством самостоятельности."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from iam.domain import new_id
from iam.session import SessionRunner
from iam.providers.codex import fake
from iam.stubs import ScriptedProvider,Reply,rest,act
from iam.world import WRITE
from integration import goal_series as series,intention_probe as probe,test_intention_probe as fixtures


class GoalSeriesTest(unittest.TestCase):
    def setUp(self):
        fixtures.IntentionProbeTest.setUp(self)
        def factory(name,transport):
            proposed=(fixtures.IntentionProbeTest.goal(self,'A'),) if name[0]=='A' else ()
            return ScriptedProvider([Reply(proposed),Reply(proposed),Reply((act(WRITE,{
                'project_id':'draft','title':'Зарисовка','text':'Свет на стекле смещается при повороте. Описание отражения.'}),))])
        result=probe.run_probe(self.source,self.output,protocol='options',provider_factory=factory)
        self.assertEqual(result['status'],'completed')
        self.continued=self.root/'continued'
        self.hashes={story:probe.series.sha(self.output/f'cells/{story}-options_visible/persona.db')for story in series.STORIES}

    def test_real_child_processes_recover_goal_without_inputs_and_preserve_sources(self):
        result=series.run_series(self.output,self.continued)
        self.assertEqual((result['status'],result['decisions'],result['model_calls']),('completed',12,0))
        self.assertIsNone(result['behavioral_success'])
        self.assertEqual(len({s['process_id']for s in result['sessions']}),6)
        self.assertNotIn(os.getpid(),{s['process_id']for s in result['sessions']})
        manifest=series.load(self.continued,'manifest.json')
        self.assertEqual((manifest['injected_inputs'],manifest['worker_mode']),(0,'subprocess'))
        self.assertEqual((manifest['max_decisions'],manifest['max_model_calls'],manifest['session_calls']),(12,60,10))
        for row in result['sessions']:
            self.assertEqual(row['recovered_goals'],manifest['initial'][row['name'][0]]['goals'])
            self.assertEqual(row['goals'],row['recovered_goals'])
        before=probe.series.sha(self.continued/'summary.json')
        self.assertEqual(series.run_session(self.continued,'A-1'),1)
        self.assertEqual(probe.series.sha(self.continued/'summary.json'),before)
        for story,sha in self.hashes.items():
            self.assertEqual(probe.series.sha(self.output/f'cells/{story}-options_visible/persona.db'),sha)

    def test_partial_keeps_only_accepted_appraisal_and_reaches_choice(self):
        def reply(query):
            if query['purpose']!='appraise':return Reply((rest(),))
            if query['allowed_proposals']==['intention']:return Reply(())
            text=next(row['text']for row in query['context']if 'Новые основания этой оценки:'in row['text'])
            bases=json.loads(text.split('Новые основания этой оценки: ',1)[1].split('. Каждое',1)[0])
            first={'proposal_id':new_id('prop'),'kind':'memory_write','rationale':'Fixture only','cites':[bases[0]],
                   'body':{'draft':{'kind':'semantic','origin':'own_hypothesis','content':{'text':'ACCEPTED_VIEW',
                     'motivation':{'topic':'draft','interest':.5,'need':.2}}}}}
            second=copy.deepcopy(first);second['proposal_id']=new_id('prop')
            second['body']['draft']['content'].update(text='REJECTED_VIEW',motivation={'topic':'draft','interest':.8,'need':.2})
            return Reply((first,second))
        result=series.run_series(self.output,self.continued,provider_factory=lambda n,t:ScriptedProvider([reply]*20))
        self.assertEqual(result['status'],'completed')
        entry=series.load(self.continued,'sessions/A-1/decisions/01.json')
        stage=next(row for row in entry['report']['stages']if row['stage']=='experience_appraisal')
        self.assertEqual((stage['status'],stage['detail']['status']),('ok','partial'))
        self.assertEqual(len(stage['detail']['rejected']),1)
        choice=entry['traces'][-1]['request']
        self.assertEqual(choice['purpose'],'choose_action')
        self.assertIn('ACCEPTED_VIEW',json.dumps(choice['context']))
        self.assertNotIn('REJECTED_VIEW',json.dumps(choice['context']))

    def test_physical_reservation_global_and_decision_caps_precede_transport(self):
        class OverCalls:
            name='fixture'
            def __init__(self,transport):self.transport=transport
            def propose(self,query):
                for _ in range(7):self.transport(('fake',),'PRIVATE_INPUT',1)
                raise AssertionError('Must stop before seventh call')
        for maximum,expected in ((60,6),(2,2)):
            target=self.root/f'cap-{maximum}'
            with patch.object(series,'MAX_CALLS',maximum),patch.object(probe,'subprocess_runner',return_value=fake.reply([])) as runner:
                result=series.run_series(self.output,target,mode='live',auth_file=str(self.root/'private.json'),
                                        provider_factory=lambda n,t:OverCalls(t))
            self.assertEqual((result['status'],result['decisions'],result['model_calls'],runner.call_count),('stopped',1,expected,expected))
            self.assertEqual(len(list(target.glob('sessions/*/physical/*.json'))),expected)
            self.assertTrue(all(row['status']=='returned'for row in result['attempts']))
            for path in target.rglob('*.json'):self.assertNotIn('PRIVATE_INPUT',path.read_text())

    def test_changed_sources_stop_before_first_decision(self):
        original=series.sources()
        with patch.object(series,'sources',side_effect=[original,{}]):
            result=series.run_series(self.output,self.continued,provider_factory=lambda n,t:ScriptedProvider())
        self.assertEqual((result['status'],result['decisions'],result['model_calls']),('stopped',0,0))

    def test_existing_output_or_mismatching_inherited_goal_is_rejected(self):
        self.continued.mkdir()
        with self.assertRaises(ValueError):series.run_series(self.output,self.continued)
        self.continued.rmdir()
        path=self.output/'cells/A-options_visible/result.json';data=series.load(path.parent,path.name)
        next(iter(data['new_intentions'].values()))['criterion']='Invented criterion'
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):series.run_series(self.output,self.continued)
        self.assertFalse(self.continued.exists())

    def test_session_deadline_expires_after_charge_without_invoking_transport(self):
        expired=False
        original_charge,original_usage=SessionRunner.charge,SessionRunner.usage
        def charge(session,*args,**kwargs):
            nonlocal expired
            value=original_charge(session,*args,**kwargs);expired=True;return value
        def usage(session):
            value=original_usage(session)
            if expired:value['elapsed_ms']=value['max_ms']+1
            return value
        class OneCall:
            name='fixture'
            def __init__(self,transport):self.transport=transport
            def propose(self,query):return self.transport(('fake',),'PRIVATE_INPUT',1)
        with patch.object(SessionRunner,'charge',charge),patch.object(SessionRunner,'usage',usage),patch.object(probe,'subprocess_runner') as runner:
            result=series.run_series(self.output,self.continued,mode='live',auth_file=str(self.root/'private.json'),
                                    provider_factory=lambda n,t:OneCall(t))
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(runner.call_count,0)
        self.assertFalse(result['attempts'][0]['transport_invoked'])
        self.assertEqual(result['attempts'][0]['status'],'error')

    def test_reflection_tool_or_stale_never_hides_behind_successful_choice(self):
        for mode in ('tool','stale'):
            def reply(query):
                if query['purpose']=='appraise':return Reply(())
                if query['purpose']=='reflect':
                    return Reply((),tool_use=mode=='tool',extra={'epoch':query['epoch']-1} if mode=='stale' else {})
                return Reply(({'proposal_id':new_id('prop'),'kind':'reflection','rationale':'Fixture opt-in','cites':[],'body':{}},))
            target=self.root/mode
            result=series.run_series(self.output,target,provider_factory=lambda n,t:ScriptedProvider([reply]*20))
            self.assertEqual((result['status'],result['decisions']),('stopped',1))
            entry=series.load(target,'sessions/A-1/decisions/01.json')
            self.assertEqual(entry['report']['outcome'],'done')
            self.assertTrue(entry['technical_faults'])

    def test_empty_choices_are_valid_declines_and_do_not_create_goals(self):
        result=series.run_series(self.output,self.continued,provider_factory=lambda n,t:ScriptedProvider([Reply(())]*20))
        self.assertEqual((result['status'],result['decisions']),('completed',12))
        self.assertIsNone(result['behavioral_success'])
        for row in result['sessions']:
            self.assertEqual(row['goals'],row['recovered_goals'])
            self.assertTrue(all(d['outcome']=='rejected' for d in row['decisions']))

    def test_undelivered_creator_request_is_visible_waiting_not_a_technical_repair(self):
        from iam.stubs import ask
        def reply(query):
            return Reply(()) if query['purpose']=='appraise' else Reply((ask('fixture_need_file','Нужен недоступный файл'),))
        result=series.run_series(self.output,self.continued,provider_factory=lambda n,t:ScriptedProvider([reply]*20))
        self.assertEqual(result['status'],'completed')
        entry=series.load(self.continued,'sessions/A-1/decisions/01.json')
        self.assertTrue(any(e['type']=='system.error' and e['payload'].get('fault')=='creator_undelivered' for e in entry['events']))
        self.assertFalse(entry['technical_faults'])
        self.assertIsNone(result['behavioral_success'])
