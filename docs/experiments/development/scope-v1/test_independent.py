"""Собственный расчёт не закрывает заданное в тесте требование нового измерения."""
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from iam.domain import new_id
from iam.stubs import Reply,ScriptedProvider

HERE=Path(__file__).resolve().parent;S=runpy.run_path(str(HERE/'test_runner.py'))


class IndependentTests(unittest.TestCase):
 setUp=S['RunnerTests'].setUp

 def test_unavailable_observation_survives_restart_without_fake_completion(self):
  def factory(runtime):
   p=ScriptedProvider()
   def choose(q):
    goals=json.loads(q['context'][-1]['text'])['development']['intentions']
    if not goals:return Reply(proposals=({'proposal_id':new_id('prop'),'kind':'intention','rationale':'Цель только для теста',
      'cites':S['refs'](q),'body':{'op':'create','id':'observe','text':'Сопоставить прогноз с новым наблюдением.',
      'operation':'write_project','target':'check','criterion':'Независимо получено новое значение.','evidence_kind':'independent_observation'}},))
    return Reply(proposals=(S['action'](q,'write_project','check',title='Расчёт',topic='investigation',text='21 вычислено по гипотезе, независимых данных нет.'),))
   p.queue(choose,choose);return p
  run=S['RUN']
  with patch.dict(run.__globals__,{'SESSIONS':2,'CHOICES':2}):run(self.source,self.root/'independent',provider_factory=factory)
  v=S['VERIFY'](self.root/'independent');self.assertTrue(v['all_completed'])
  for row in v['runs']:
   goal=row['assessment']['intention_candidates'][0]
   self.assertEqual(goal['final_status'],'active');self.assertFalse(goal['transitions'])
   t=json.loads((self.root/'independent'/row['code']/'trace.json').read_text())
   for d in t['decision_trace'][1:]:
    view=json.loads(d['requests'][0]['context'][-1]['text']);item=view['motivation']['goal_opportunities'][0]
    self.assertTrue(item['operation_available']);self.assertFalse(item['evidence_available'])
    self.assertEqual(item['goal_component'],0.0)
    self.assertFalse(any(x['completion_for'] for x in json.loads(d['requests'][0]['context'][-2]['text'])['experience_sources'].values()))
