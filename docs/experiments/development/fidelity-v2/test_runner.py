"""Заглушечные цели проверяют стенд, не являются живыми свидетельствами."""
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from iam.domain import new_id
from iam.experiments.fidelity import INSTRUCTION
from iam.interfaces import ProviderError
from iam.stubs import Reply, ScriptedProvider, act, rest

HERE=Path(__file__).resolve().parent
RUN=runpy.run_path(str(HERE/'run.py'))['run']
PREPARE=runpy.run_path(str(HERE.parent/'scope-v1/prepare.py'))['prepare']
VERIFY=runpy.run_path(str(HERE/'verify.py'))['check']


def refs(q):return list(dict.fromkeys(c['ref'] for c in q['context']))
def silence(q):return Reply(proposals=(rest('Контрольное бездействие заглушки',cites=refs(q)),))
def action(q,op,target='',**kw):return act('cap_ExperimentDevelopment01',{'operation':op,'target':target,**kw},cites=refs(q))


class RunnerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.source=self.root/'sources';PREPARE(self.source)

 def execute(self,factory,name='live'):
  with patch.dict(RUN.__globals__,{'SESSION_COUNTS':{'F':2,'N':2,'E':2,'CF':2,'CN':2},'CHOICES':2}):return RUN(self.source,self.root/name,provider_factory=factory)

 def test_neutral_sources_fresh_sessions_and_no_goal_are_valid(self):
  before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.rglob('*') if p.is_file()}
  providers=[];requests=[]
  def factory(runtime):
   p=ScriptedProvider();providers.append(p)
   for _ in range(2):p.queue(lambda q:(requests.append(q),silence(q))[1])
   return p
  s=self.execute(factory)
  self.assertEqual([r['code'] for r in s['runs']],['F-1','N-1','E-1'])
  self.assertEqual(len({id(p) for p in providers}),6)
  self.assertTrue(all(q['context'][0]['text']==INSTRUCTION and q['purpose']=='choose_action' for q in requests))
  self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in before.items()))
  v=VERIFY(self.root/'live');self.assertTrue(v['all_completed']);self.assertEqual(v['counts']['restored_sessions'],6)
  self.assertTrue(all(not r['assessment']['intention_candidates'] for r in v['runs']))

 def test_future_read_and_write_survive_restart_and_verifier_checks_quotes(self):
  for op,target,quote in [('write_project','notes','Наблюдение отделено от вывода.'),('read_material','patterns','Разности растут на единицу.')]:
   def factory(runtime):
    had_goal=bool(runtime.controller.state['intentions']);p=ScriptedProvider()
    def choose(q):
     if q['purpose']=='appraise':
      packet=json.loads(q['context'][1]['text'])
      if set(packet)=={'goal'}:
       value=dict(required_kind='received_text' if packet['goal']['operation']=='read_material' else 'artifact',support_quote=packet['goal']['text'],reason='Тестовая классификация.')
       pr=rest('',cites=[q['context'][1]['ref']]);pr['body']=value
       return Reply(proposals=(pr,))
      packet=json.loads(q['context'][1]['text']);v=dict(verdict='pass',goal_met=True,criterion_met=True,evidence_sufficient=True,support_quote=quote,reason='Контроль заглушки.')
      pr=rest('',cites=[q['context'][1]['ref']]);pr['body']=v
      return Reply(proposals=(pr,))
     view=json.loads(q['context'][-1]['text'])['development'];goals=view['intentions']
     if not goals:
      goal={'proposal_id':new_id('prop'),'kind':'intention','rationale':'Будущий замысел заглушки','cites':refs(q),
            'body':{'op':'create','id':'later','text':'Контроль будущего результата','operation':op,'target':target,'criterion':'Получен контрольный результат.','evidence_kind':'artifact' if op=='write_project' else 'received_text'}}
      return Reply(proposals=(goal,action(q,'rest')))
     if goals['later']['status']=='done' or not had_goal:return silence(q)
     sources=json.loads(q['context'][-2]['text'])['experience_sources'];eligible=[ref for ref,item in sources.items() if 'later' in item['completion_for']]
     if eligible:
      goal={'proposal_id':new_id('prop'),'kind':'intention','rationale':'Подтверждённый контрольный результат','cites':refs(q),
            'body':{'op':'transition','id':'later','to_status':'done','result_ref':eligible[0],'result_quote':quote}}
      return Reply(proposals=(goal,action(q,'rest')))
     kw={'title':'Заметки','topic':'investigation','text':quote} if op=='write_project' else {}
     return Reply(proposals=(action(q,op,target,**kw),))
    p.queue(choose,choose,choose,choose);return p
   self.execute(factory,op);v=VERIFY(self.root/op)
   for row in v['runs']:
    g=row['assessment']['intention_candidates'][0]
    self.assertEqual(g['final_status'],'done');self.assertEqual(len(g['matching_later_actions']),1)
    self.assertEqual(g['operation'],op);self.assertEqual(g['transitions'][0]['result_quote'],quote)
    self.assertEqual(g['specificity'],'requires_review')

 def test_source_tamper_fails_before_calls_and_provider_error_stops_all(self):
  class Broken:
   max_format_retries=0
   def propose(self,q):raise ProviderError('Контрольный отказ')
  s=self.execute(lambda _:Broken());self.assertEqual(s['runs'][0]['calls'],1)
  self.assertEqual(s['runs'][0]['status'],'technical_pause');self.assertTrue(all(r['status']=='not_run' for r in s['runs'][1:]))
  self.assertFalse(VERIFY(self.root/'live')['all_completed'])
  runpy.run_path(str(HERE/'export.py'))['export'](self.root/'live',self.root/'published')
  self.assertIn('Не выполнено:',(self.root/'published/EPISODES.md').read_text())
  data=json.loads((self.source/'summary.json').read_text());data['runs'][1]['sha256']='0'*64
  (self.source/'summary.json').write_text(json.dumps(data))
  with self.assertRaises(ValueError):self.execute(lambda _:Broken(),'tampered')
  self.assertFalse((self.root/'tampered').exists())

 def test_continuations_preserve_own_goal_without_reassignment(self):
  old_run=runpy.run_path(str(HERE.parent/'commitment-v1/run.py'))['run']
  def old_factory(runtime):
   p=ScriptedProvider(name='codex',model='gpt-6-luna')
   def create(q):
    goal={'proposal_id':new_id('prop'),'kind':'intention','rationale':'Собственный выбор заглушки','cites':refs(q),
      'body':{'op':'create','id':'later','text':'Написать продолжение.','operation':'write_project','target':'draft','criterion':'Есть следующий эпизод.'}}
    return Reply(proposals=(goal,action(q,'rest')))
   p.queue(create);return p
  historical=self.root/'historical_fixture'
  with patch.dict(old_run.__globals__,{'ORDER':(('F',1),('N',1)),'SESSIONS':1,'CHOICES':1}):
   fixture=old_run(self.source,historical,provider_factory=old_factory)
  # Только временная заглушечная fixture: проверяем контракт допуска исторических записей.
  # Это НЕ настоящая live-серия и не попадает в публикуемые свидетельства.
  fixture['live']=True
  (historical/'summary.json').write_text(json.dumps(fixture))
  factory=lambda _:ScriptedProvider([silence,silence])
  with patch.dict(RUN.__globals__,{'SESSION_COUNTS':{'F':2,'N':2,'E':2,'CF':2,'CN':2},'CHOICES':2}):
   summary=RUN(self.source,self.root/'continued',provider_factory=factory,continuation_root=historical)
  verified=VERIFY(self.root/'continued')
  self.assertEqual(verified['counts']['restored_sessions'],10)
  self.assertEqual(summary['sources']['CF']['source_kind'],'natural_live_checkpoint')
  self.assertEqual(summary['sources']['CF']['initial_intentions'],1)
  self.assertTrue(verified['runs'][-1]['assessment']['intention_candidates'][0]['inherited'])
  self.assertEqual(verified['runs'][-1]['assessment']['intention_candidates'][0]['final_status'],'active')
  fixture['live']=False;(historical/'summary.json').write_text(json.dumps(fixture))
  with self.assertRaises(AssertionError):RUN(self.source,self.root/'bad_fixture',provider_factory=factory,continuation_root=historical)

 def test_scope_mismatch_is_normal_rejection_not_technical_pause(self):
  def factory(runtime):
   def choose(q):
    packet=json.loads(q['context'][1]['text']) if q['purpose']=='appraise' else None
    if packet:
     assert set(packet)=={'goal'},'результат не должен предъявляться при несовпадении'
     value=dict(required_kind='independent_observation',support_quote=packet['goal']['text'],reason='Нужно независимое значение.')
     pr=rest('',cites=[q['context'][1]['ref']]);pr['body']=value;return Reply(proposals=(pr,))
    goals=json.loads(q['context'][-1]['text'])['development']['intentions']
    if not goals:
     body=dict(op='create',id='later',text='Проверить прогноз по независимо полученному значению.',operation='write_project',
       target='notes',criterion='В заметке есть число.',evidence_kind='artifact')
    else:
     sources=json.loads(q['context'][-2]['text'])['experience_sources'];eligible=[k for k,v in sources.items() if 'later' in v['completion_for']]
     if not eligible:return Reply(proposals=(action(q,'write_project','notes',title='Заметка',topic='x',text='Собственный расчёт21.'),))
     body=dict(op='transition',id='later',to_status='done',result_ref=eligible[0],result_quote='Собственный расчёт21.')
    return Reply(proposals=({'proposal_id':new_id('prop'),'kind':'intention','rationale':'Заглушечный случай','cites':refs(q),'body':body},))
   return ScriptedProvider([choose]*4)
  summary=self.execute(factory);self.assertFalse(summary['provider_paused'])
  v=VERIFY(self.root/'live');self.assertTrue(v['all_completed']);self.assertEqual(v['counts']['checked_assessments'],6)
  for row in v['runs']:self.assertEqual(row['assessment']['intention_candidates'][0]['final_status'],'active')
