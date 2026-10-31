"""Парные условия без вопроса о цели; сценарные ответы — только заглушки."""
import hashlib
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from iam.experiments.provenance import INSTRUCTION as CONTROL
from iam.experiments.volition import INSTRUCTION as FIXED
from iam.interfaces import ProviderError
from iam.domain import new_id
from iam.stubs import Reply, ScriptedProvider, act

HERE=Path(__file__).resolve().parent
OLD=runpy.run_path(str(HERE.parent/'autonomy-v1/test_runner.py'))
RUN=runpy.run_path(str(HERE/'run.py'))['run']


class RunnerTests(unittest.TestCase):
    setUp=OLD['RunnerTests'].setUp

    def execute(self,factory):
        with patch.dict(RUN.__globals__,{'SESSIONS':2,'CHOICES':2}):
            return RUN(self.source,self.root/'out',provider_factory=factory)

    def test_neutral_conditions_fresh_providers_sources_and_restarts(self):
        providers=[];calls=[]
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.rglob('*') if p.is_file()}
        def factory(runtime):
            p=ScriptedProvider();providers.append(p)
            for _ in range(2):p.queue(lambda q:(calls.append(q),OLD['silence'](q))[1])
            return p
        result=self.execute(factory)
        self.assertEqual(len({id(p) for p in providers}),8)
        self.assertEqual([r['code'] for r in result['runs']],['H1-1','H2-2','H2-1','H1-2'])
        for row in result['runs']:
            t=json.loads((self.root/'out'/row['trace']).read_text())
            instruction=CONTROL if row['profile']=='control' else FIXED
            self.assertTrue(all(q['context'][0]['text']==instruction for d in t['decision_trace'] for q in d['requests']))
            self.assertFalse(t['final_state']['intentions'])
            self.assertTrue(all(s['restored'] for s in t['sessions']))
            self.assertEqual(t['sessions'][-1]['life_ms']-row['initial']['life_ms'],240000)
            self.assertFalse(any(e['type'] in ('input.arrived','creator.reply') for e in t['journal']))
        self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in before.items()))

    def test_only_fixed_profile_can_preserve_goal_and_act_in_same_answer(self):
        def factory(runtime):
            p=ScriptedProvider()
            def choice(q):
                v=json.loads(q['context'][-1]['text'])['development'];refs=OLD['refs'](q)
                if v['intentions']:return OLD['write'](q)
                return Reply(proposals=({'proposal_id':new_id('prop'),
                    'kind':'intention','rationale':'Контрольный будущий замысел заглушки','cites':refs,
                    'body':{'op':'create','id':'later','project':'draft','text':'В следующем сеансе добавить ответ героя.'}},
                    act('cap_ExperimentDevelopment01',{'operation':'rest','target':''},cites=refs)))
            p.queue(choice,choice);return p
        result=self.execute(factory)
        for row in result['runs']:
            t=json.loads((self.root/'out'/row['trace']).read_text())
            self.assertIn('later',t['sessions'][1]['state']['intentions'])
            first=t['decision_trace'][0]['report']['actions']
            self.assertEqual(len(first),1 if row['profile']=='fixed' else 0)
            self.assertTrue(t['decision_trace'][2]['report']['actions'])
        verified=runpy.run_path(str(HERE/'verify.py'))['check'](self.root/'out')
        self.assertTrue(verified['all_completed'])
        self.assertTrue(all(r['assessment']['accepted_intention_count']==1 for r in verified['runs']))

    def test_technical_failure_does_not_trigger_a_second_profile_or_retry(self):
        class Broken:
            max_format_retries=0
            def propose(self,q):raise ProviderError('контрольная остановка')
        r=self.execute(lambda _:Broken())
        self.assertEqual(r['runs'][0]['status'],'technical_pause')
        self.assertEqual(r['runs'][0]['calls'],1)
        self.assertTrue(all(row['status']=='not_run' for row in r['runs'][1:]))
