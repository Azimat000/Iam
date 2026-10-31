"""Порядок неприоритетных целей не меняет смысл, значения и дубликаты проверяются."""
import copy
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from iam.domain import new_id
from iam.state import StateController,replay
from iam.storage import Store
from iam.stubs import Reply,ScriptedProvider

HERE=Path(__file__).resolve().parent
S=runpy.run_path(str(HERE/'test_runner.py'))


class OrderTests(unittest.TestCase):
 setUp=S['RunnerTests'].setUp

 def test_creation_order_and_recovered_order_are_nonranking_but_values_are_strict(self):
  def factory(runtime):
   p=ScriptedProvider()
   def choice(q):
    goals=json.loads(q['context'][-1]['text'])['development']['intentions']
    if len(goals)>=2:return S['silence'](q)
    ident='z_goal' if not goals else 'a_goal'
    return Reply(proposals=({'proposal_id':new_id('prop'),'kind':'intention','rationale':'Контроль порядка заглушки',
      'cites':S['refs'](q),'body':{'op':'create','id':ident,'text':'Продолжить работу в будущем.','operation':'write_project',
      'target':'draft','criterion':'Будет записан контрольный текст.','evidence_kind':'artifact'}},))
   p.queue(choice,choice,choice);return p
  run=S['RUN']
  with patch.dict(run.__globals__,{'SESSIONS':2,'CHOICES':3}):run(self.source,self.root/'order',provider_factory=factory)
  result=S['VERIFY'](self.root/'order');self.assertTrue(result['all_completed'])
  check=runpy.run_path(str(HERE/'signals.py'))['check_signals']
  with Store.open(self.root/'order/F-1/persona.db',readonly=True) as store:
   c=StateController(store);events=c.events_after(0);mapping={e.event_id:e for e in events}
   q=next(e.payload['request'] for e in events if e.type=='proposal.requested' and
      [i['id'] for i in json.loads(e.payload['request']['context'][-1]['text']).get('motivation',{}).get('goal_opportunities',[])]==['z_goal','a_goal'])
   state=store.get_snapshot(q['epoch'],q['state_version']).state
   memory=next(r.memory for r in replay(store) if (r.epoch,r.state_version)==(q['epoch'],q['state_version']))
   self.assertGreater(check(q,state,memory,mapping),0)
   reordered=copy.deepcopy(q);view=json.loads(q['context'][-1]['text']);view['motivation']['goal_opportunities'].reverse()
   for item in view['motivation']['items']:item['accepted_goals'].reverse()
   reordered['context'][-1]['text']=json.dumps(view);self.assertGreater(check(reordered,state,memory,mapping),0)
   for defect in ('value','duplicate'):
    broken=copy.deepcopy(q);view=json.loads(q['context'][-1]['text']);rows=view['motivation']['goal_opportunities']
    if defect=='value':rows[0]['goal_component']+=0.1
    else:rows[0]['id']=rows[1]['id']
    broken['context'][-1]['text']=json.dumps(view)
    with self.assertRaises(AssertionError):check(broken,state,memory,mapping)
