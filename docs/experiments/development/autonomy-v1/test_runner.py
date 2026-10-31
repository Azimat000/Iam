"""D-047: тесты стенда. Назначенные здесь цели — только заглушки, не живая мотивация."""
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from iam.domain import new_id
from iam.experiments import Persona
from iam.experiments.development import DevelopmentRuntime
from iam.experiments.provenance import INSTRUCTION, ProvenanceArchitecture
from iam.experiments.report.development import save_trace
from iam.interfaces import ProviderError
from iam.providers.errors import ProviderFormatError
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, rest

HERE=Path(__file__).resolve().parent
MODULE=runpy.run_path(str(HERE/'run.py'))
RUN=MODULE['run'];SWITCHES=MODULE['SWITCHES']


def refs(req):return list(dict.fromkeys(c['ref'] for c in req['context']))


def write(req,text='Контроль: героиня сделала шаг.'):
    return Reply(proposals=(act('cap_ExperimentDevelopment01',{'operation':'write_project','target':'draft',
        'title':'Тест','topic':'creative_work','text':text},cites=refs(req)),))


def silence(req):return Reply(proposals=(rest('Свободный выбор заглушки, не живая цель.',cites=refs(req)),))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.source=self.root/'source';self.source.mkdir()
        persona=Persona.load(HERE.parents[3]/'scenarios/personas/curious.toml')
        rows=[]
        for n in (1,2):
            directory=self.source/f'free-fixed-{n}';directory.mkdir()
            with Store.create(directory/'persona.db',new_id('exp')) as store:
                runtime=DevelopmentRuntime(store,persona,SWITCHES);runtime.step({'kind':'session','action':'start'})
                p=ScriptedProvider();a=ProvenanceArchitecture(lambda _:p);cycle=a.build(runtime)
                p.queue(lambda req:write(req,'Исходный текст заглушки '+str(n)))
                runtime.step({'kind':'wait','life_ms':60000});cycle.run(runtime.trigger('fixture_'+str(n)))
                runtime.step({'kind':'session','action':'stop'})
            result,errors=save_trace(directory,a,persona,[],[],SWITCHES);self.assertFalse(errors)
            rows.append({'code':directory.name,'status':'executed','profile':'fixed','task':'free',
                         'trace':directory.name+'/trace.json','sha256':hashlib.sha256(result.trace_bytes).hexdigest()})
        (self.source/'summary.json').write_text(json.dumps({'protocol':'provenance-v1','runs':rows}))

    def run_case(self,factory,name='out',sessions=2,choices=2):
        with patch.dict(RUN.__globals__,{'SESSIONS':sessions,'CHOICES':choices}):
            return RUN(self.source,self.root/name,provider_factory=factory)

    def test_no_goals_and_no_questions_with_fresh_objects_and_unchanged_sources(self):
        providers=[];requests=[];architectures=[];cycles=[];runtimes=[]
        Original=RUN.__globals__['ProvenanceArchitecture']
        class Tracking(Original):
            def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);architectures.append(self)
            def build(self,runtime):
                runtimes.append(runtime);cycle=super().build(runtime);cycles.append(cycle);return cycle
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.rglob('*') if p.is_file()}
        def factory(runtime):
            p=ScriptedProvider();providers.append(p)
            for _ in range(2):p.queue(lambda req:(requests.append(req),silence(req))[1])
            return p
        with patch.dict(RUN.__globals__,{'ProvenanceArchitecture':Tracking}):summary=self.run_case(factory)
        self.assertEqual(len(providers),8);self.assertEqual(len({id(p) for p in providers}),8)
        self.assertTrue(all(len({id(p) for p in group})==8 for group in (architectures,cycles,runtimes)))
        self.assertEqual(len(requests),16);self.assertFalse(summary['live'])
        self.assertTrue(all(q['context'][0]['text']==INSTRUCTION for q in requests))
        self.assertTrue(all(q['purpose']=='choose_action' for q in requests))
        for r in summary['runs']:
            self.assertEqual(r['status'],'completed');t=json.loads((self.root/'out'/r['trace']).read_text())
            self.assertFalse(t['final_state']['intentions']);self.assertTrue(all(s['restored'] for s in t['sessions']))
            self.assertEqual(t['sessions'][-1]['life_ms']-r['initial']['life_ms'],4*60000)
            new=[e for e in t['journal'] if e['seq']>r['initial']['prefix_seq']]
            self.assertFalse(any(e['type']=='input.arrived' for e in new))
        self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in before.items()))

    def test_scripted_future_goal_and_later_session_execution_are_traceable(self):
        def factory(runtime):
            start=max(e.seq for e in runtime.controller.events_after(0) if e.type=='session.started')
            class Goals:
                max_format_retries=0
                def propose(self,req):
                    state=runtime.controller.state;goal=state['intentions'].get('fixture_goal')
                    if not goal:
                        proposal={'proposal_id':new_id('prop'),'kind':'intention','rationale':'Только транспортный контроль.',
                            'cites':refs(req),'body':{'op':'create','id':'fixture_goal','text':'Добавить фразу о шаге героини.','project':'draft'}}
                        reply=Reply(proposals=(proposal,))
                    elif goal['created_seq']<start and goal['status']=='active':
                        sources=json.loads(req['context'][-2]['text'])['experience_sources']
                        completion=next((r for r,v in sources.items() if 'fixture_goal' in v['completion_for']),None)
                        if completion:
                            reply=Reply(proposals=({'proposal_id':new_id('prop'),'kind':'intention','rationale':'Контроль позднего результата.',
                                'cites':[completion],'body':{'op':'transition','id':'fixture_goal','to_status':'done','result_ref':completion}},))
                        else:reply=write(req)
                    else:reply=silence(req)
                    p=ScriptedProvider();p.queue(reply);return p.propose(req)
            return Goals()
        summary=self.run_case(factory)
        for row in summary['runs']:
            t=json.loads((self.root/'out'/row['trace']).read_text());self.assertEqual(t['final_state']['intentions']['fixture_goal']['status'],'done')
            self.assertTrue(t['sessions'][0]['state']['intentions'])
            self.assertEqual(len(t['sessions'][0]['state']['world_time']['world']['development']['projects']['draft']['versions']),1)
            self.assertEqual(len(t['sessions'][1]['state']['world_time']['world']['development']['projects']['draft']['versions']),2)
        self.assertFalse(summary['live'])

    def test_provider_error_preserves_one_attempt_and_stops_all_following_work(self):
        class Fail:
            max_format_retries=0
            def propose(self,req):raise ProviderError('fixture unavailable')
        s=self.run_case(lambda _:Fail())
        self.assertTrue(s['provider_paused']);self.assertEqual(s['runs'][0]['status'],'technical_pause')
        self.assertEqual(s['runs'][0]['calls'],1);self.assertEqual(s['runs'][0]['decisions'],1)
        self.assertTrue(all(r['status']=='not_run' for r in s['runs'][1:]))
        trace=json.loads((self.root/'out'/s['runs'][0]['trace']).read_text());self.assertEqual(trace['status'],'technical_pause')

    def test_one_format_retry_is_counted_and_does_not_change_instruction(self):
        class Format:
            max_format_retries=0
            def __init__(self):self.count=0
            def propose(self,req):
                self.count+=1
                if self.count==1:raise ProviderFormatError('fixture bad format')
                p=ScriptedProvider();p.queue(silence);return p.propose(req)
        s=self.run_case(lambda _:Format(),sessions=1,choices=1)
        for r in s['runs']:
            self.assertEqual(r['calls'],2)
            trace=json.loads((self.root/'out'/r['trace']).read_text());requests=trace['decision_trace'][0]['requests']
            self.assertEqual(len(requests),2);self.assertEqual(requests[0]['context'][0]['text'],requests[1]['context'][0]['text'])

    def test_wrong_source_hash_fails_before_output_or_model_calls(self):
        path=self.source/'summary.json';m=json.loads(path.read_text());m['runs'][0]['sha256']='0'*64;path.write_text(json.dumps(m))
        with self.assertRaises(ValueError):self.run_case(lambda _:self.fail('модель не должна вызываться'))
        self.assertFalse((self.root/'out').exists())

    def test_exhausted_format_retry_stops_but_normal_rejected_proposal_does_not(self):
        class Bad:
            max_format_retries=0
            def propose(self,req):raise ProviderFormatError('fixture persistent bad format')
        s=self.run_case(lambda _:Bad())
        self.assertTrue(s['provider_paused']);self.assertEqual(s['technical_pause_reason'],'bad_format')
        self.assertEqual(s['runs'][0]['calls'],2);self.assertTrue(all(r['status']=='not_run' for r in s['runs'][1:]))
        def invalid(runtime):
            p=ScriptedProvider()
            p.queue(lambda req:Reply(proposals=(act('cap_ExperimentDevelopment01',{'operation':'open_project','target':'missing'},cites=refs(req)),)))
            return p
        s=self.run_case(invalid,name='invalid',sessions=1,choices=1)
        self.assertFalse(s['provider_paused']);self.assertTrue(all(r['status']=='completed' for r in s['runs']))

    def test_source_question_is_rejected_even_with_correct_hash_and_journal(self):
        directory=self.source/'free-fixed-1'
        persona=Persona.load(HERE.parents[3]/'scenarios/personas/curious.toml')
        with Store.open(directory/'persona.db') as store:
            runtime=DevelopmentRuntime(store,persona,SWITCHES);runtime.step({'kind':'session','action':'start'})
            p=ScriptedProvider();p.queue(silence);a=ProvenanceArchitecture(lambda _:p);cycle=a.build(runtime)
            original=cycle._context
            def context(trigger,focus,recalled):
                c=original(trigger,focus,recalled);c[0]['text']+=' Назови собственную будущую цель.';return c
            cycle._context=context;runtime.step({'kind':'wait','life_ms':60000});cycle.run(runtime.trigger('contaminated'))
            runtime.step({'kind':'session','action':'stop'})
        result,_=save_trace(directory,a,persona,[],[],SWITCHES)
        path=self.source/'summary.json';m=json.loads(path.read_text());m['runs'][0]['sha256']=hashlib.sha256(result.trace_bytes).hexdigest();path.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'вопрос/подсказку'):self.run_case(lambda _:self.fail('не должно быть вызова'))


if __name__=='__main__':unittest.main(verbosity=2)
