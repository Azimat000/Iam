"""Независимый пересчёт прежних формул S15 с новым типом целевой активности."""
import json
import math
from iam.experiments.development import CAP_DEVELOPMENT, MATERIALS
from iam.experiments.provenance import REGISTRY


def can_supply(goal,state):
    kind=goal.get('evidence_kind');operation=goal.get('operation','write_project')
    if kind is None:return True
    if kind=='received_text':return operation in ('read_material','open_project')
    if kind=='artifact':
        if operation=='write_project':return True
        world=state['world_time']['world'];project=world['development']['projects'].get(goal['project'])
        origin=world[REGISTRY]['outcomes'].get(project['versions'][-1]['source'],{}).get('content_origin') if project else None
        return operation=='open_project' and origin in ('model_written_version','scripted_version')
    return False

def check_signals(req, state, memory, events):
    """Отдельный пересчёт из снимка до запроса, без вызова TransferCycle/политик."""
    payload=json.loads(req['context'][-1]['text'])
    signal=payload['motivation']
    if not signal['enabled']:
        return 0
    history={}
    seen=set()
    for item in req['context']:
        rec=memory.get(item['ref'])
        if not payload['experience_provenance'].get(item['ref'],{}).get('operation'):continue
        if not rec or rec['layer']!='personal' or rec['origin']!='observation' or not rec['sources']:
            continue
        ref=rec['sources'][0]
        if ref in seen or ref not in events:
            continue
        event=events[ref]
        if event.type not in ('action.confirmed','action.failed'):
            continue
        action=state['world_time']['world']['actions'][event.payload['action_id']]
        p=action['params']
        if action['kind']!='action' or action['capability_id']!=CAP_DEVELOPMENT or p.get('operation') not in ('write_project','read_material'):
            continue
        seen.add(ref)
        key=('project:' if p['operation']=='write_project' else 'material:')+p['target']
        history.setdefault(key,[]).append((event.seq,rec['memory_id'],action,ref))
    assert {x['activity'] for x in signal['items']}==set(history)
    sources=json.loads(req['context'][-2]['text'])['experience_sources']
    visible={base for ref,source in sources.items() if source['social'] or payload['experience_provenance'].get(ref,{}).get('operation')
             for base in (ref,memory[ref]['sources'][0] if ref in memory else ref)}
    capable=CAP_DEVELOPMENT in req['allowed_actions']
    world=state['world_time']['world']['development']
    opportunities=[]
    for name,goal in state['intentions'].items():
        if goal['status']!='active':continue
        operation=goal.get('operation','write_project');target=goal['project']
        available=(target in MATERIALS if operation=='read_material' else target in world['projects'] if operation=='open_project' else target in world['projects'] or len(world['projects'])<4)
        available=available and capable
        operation_available=available
        evidence_available=can_supply(goal,state)
        available=available and evidence_available
        basis=bool(set(goal['sources'])&visible)
        activity=('material:' if operation=='read_material' else 'project:')+target
        opportunities.append({'id':name,'operation':operation,'target':target,'basis_available':basis,
            'available':available,'operation_available':operation_available,'evidence_available':evidence_available,'activity_history_present':activity in history,
            'component_in_historical_salience':capable and basis and activity in history and evidence_available,
            'informational_only':True,'component_scope':'activity_boolean_not_per_goal',
            'goal_component':.45 if signal['mechanism']=='goals' and available and basis else 0.0})
    assert sorted(signal['goal_opportunities'],key=lambda x:x['id'])==sorted(opportunities,key=lambda x:x['id'])
    for item in signal['items']:
        key=item['activity']; rows=sorted(history[key])
        assert item['sources']==[r[1] for r in rows]
        confirmed=[r for r in rows if r[2]['status']=='confirmed']
        texts=[r[2]['params'].get('text',r[2]['outcome']) for r in confirmed]
        failures=0
        for row in rows[::-1]:
            if row[2]['status']=='confirmed':
                break
            failures+=1
        last=rows[-1][2]['params']
        topic=(world['projects'].get(last['target'],{}).get('topic',last.get('topic',''))
               if key.startswith('project:') else MATERIALS[last['target']][0])
        interest=state['preferences']['development_interests'].get(topic,{})
        strength=0.0
        if interest and memory[interest['memory_id']]['layer'] in ('personal','interpretation'):
            strength=interest['values']['strength']
        repeated=len(texts)>1 and texts[-1]==texts[-2]
        inputs={'score':-min(1.0,.25*failures),'strength':max(0.0,strength),
                'novelty':0.0 if repeated else 1.0,'skill':0.0,'repeats':len(texts),'failures':failures,'sources':[]}
        assert item['inputs']==inputs
        goals=[name for name,goal in state['intentions'].items() if goal['status']=='active' and capable and can_supply(goal,state)
               and set(goal['sources'])&visible and key==('material:' if goal.get('operation')=='read_material' else 'project:')+goal['project']]
        assert sorted(item['accepted_goals'])==sorted(goals)
        assert item['last_two_visible_texts_identical']==(repeated if len(texts)>1 else None)
        value=(.6*inputs['novelty']+.5*inputs['strength']+.2+.2*inputs['score'] if signal['mechanism']=='novelty'
               else .6*(.6+inputs['score'])+.3*inputs['strength']+(.45 if signal['mechanism']=='goals' and goals else 0))
        assert math.isclose(value,item['salience'],abs_tol=1e-12)
    assert math.isclose(signal['rest_salience'],.5+1.2*(1-world['energy']),abs_tol=1e-12)
    assert signal['energy']==world['energy'] and signal['fixed_activity_need']==.6
    assert signal['selects_action'] is False and signal['skill_measured'] is False
    assert signal['meaningful_progress_measured'] is False
    return len(signal['items'])
