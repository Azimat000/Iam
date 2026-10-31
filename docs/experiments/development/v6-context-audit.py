from pathlib import Path
import json,sys
from iam.storage import Store
from iam.state.rules import INTENTION_MACHINE

root=Path(sys.argv[1]);rows=[]
for path in sorted(root.rglob('persona.db')):
 requests_checked=0
 with Store.open(path,readonly=True) as store:
  events=list(store.events());event_map={event.event_id:event for event in events}
  for event in events:
   if event.type!='proposal.requested':continue
   request=event.payload['request'];context=request['context']
   view=json.loads(context[-1]['text'])['development']
   sources=json.loads(context[-2]['text'])['experience_sources']
   state=store.get_snapshot(request['epoch'],request['state_version']).state
   projects=state['world_time']['world']['development']['projects'];goals=state['intentions']
   assert view['project_creation']=={'can_create':len(projects)<4,'remaining':4-len(projects),'key_pattern':'[a-z][a-z0-9_]{0,47}','operation':'write_project'}
   assert {item['target'] for item in view['available_actions'] if item['operation']=='open_project'}==set(projects)
   assert {item['target'] for item in view['available_actions'] if item['operation']=='write_project' and 'target' in item}==set(projects)
   assert any(item.get('target_rule')=='choose_new_key' for item in view['available_actions'])==(len(projects)<4)
   visible={item['ref'] for item in context};assert set(sources)<=visible
   for ref,source in sources.items():
    expected=[];result=event_map.get(ref)
    if result and result.type=='action.confirmed':
     action=state['world_time']['world']['actions'][result.payload['action_id']]
     for key,goal in view['intentions'].items():
      actual=goals[key]
      if (actual['status']=='active' and 'created_seq' in actual and result.seq>actual['created_seq']
          and action['status']=='confirmed' and action['params']['operation']=='write_project'
          and action['params']['target']==actual['project']):expected.append(key)
    assert sorted(source['completion_for'])==sorted(expected)
   assert set(view['intention_options'])==set(view['intentions'])
   for key,goal in view['intentions'].items():
    assert goal=={field:goals[key][field] for field in ('text','project','status')}
    expected=set(INTENTION_MACHINE.table[goal['status']])
    if not any(key in source['completion_for'] for source in sources.values()):expected.discard('done')
    assert set(view['intention_options'][key])==expected
   anchors=[]
   for item in context:
    if item['origin']!='own_hypothesis' or item['layer']!='state':continue
    parsed=json.loads(item['text']);key=parsed['intention'];anchors.append(key)
    assert key in view['intentions']
    actual=goals[key];anchor=event_map[item['ref']]
    assert anchor.type=='proposal.accepted' and anchor.payload['proposal_id']==actual['proposal_id']
    assert parsed=={'intention':key,**view['intentions'][key]}
   assert set(anchors)==set(view['intentions'])
   requests_checked+=1
 rows.append({'database':str(path.relative_to(root)),'requests_checked':requests_checked,'status':'pass'})
print(json.dumps({'databases_checked':len(rows),'requests_checked':sum(row['requests_checked'] for row in rows),'basis':'Сверка по снимкам версии каждого исходного запроса и исходным событиям; повторённое прошлое копий учитывается в каждой базе.','checks':rows},ensure_ascii=False,indent=2))
