"""Независимая сверка исходных баз, запросов и работ серии D-040 (read-only).

PYTHONPATH=src .venv/bin/python docs/experiments/development/transfer-v1/verify.py /каталог/серии
"""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import runpy
import sys

from iam.backup import SecretScanner
from iam.experiments.results import Normalizer
from iam.experiments.development import CAP_DEVELOPMENT, MATERIALS
from iam.state import StateController, replay, verify
from iam.storage import Store

ROOT=Path(__file__).resolve().parents[4]
helpers=runpy.run_path(str(ROOT/'scripts/experiments/verify_development.py'))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


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
    visible={ref for rows in history.values() for row in rows for ref in (row[1],row[3])}
    world=state['world_time']['world']['development']
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
        goals=[name for name,goal in state['intentions'].items() if goal['status']=='active'
               and set(goal['sources'])&visible and key=='project:'+goal['project']]
        assert item['accepted_goals']==goals
        assert item['last_two_visible_texts_identical']==(repeated if len(texts)>1 else None)
        value=(.6*inputs['novelty']+.5*inputs['strength']+.2+.2*inputs['score'] if signal['mechanism']=='novelty'
               else .6*(.6+inputs['score'])+.3*inputs['strength']+(.45 if signal['mechanism']=='goals' and goals else 0))
        assert math.isclose(value,item['salience'],abs_tol=1e-12)
    assert math.isclose(signal['rest_salience'],.5+1.2*(1-world['energy']),abs_tol=1e-12)
    assert signal['energy']==world['energy'] and signal['fixed_activity_need']==.6
    assert signal['selects_action'] is False and signal['skill_measured'] is False
    assert signal['meaningful_progress_measured'] is False
    return len(signal['items'])


def check(directory):
    root=Path(directory)
    summary=json.loads((root/'summary.json').read_text())
    expected={(s,m,r) for s in ('interrupted','repetition','failure') for m in ('needs','novelty','goals','off') for r in (1,2)}
    keys=[(r['scene'],r['mode'],r['repeat']) for r in summary['runs']]
    assert len(keys)==24 and len(set(keys))==24 and set(keys)==expected
    counts=Counter()
    rows=[]
    for row in summary['runs']:
        if row['status']!='executed':
            counts['not_run']+=1
            continue
        path=root/row['trace']
        raw=path.read_bytes()
        assert sha(raw)==row['sha256']
        assert not SecretScanner(assignments=False).find(raw.decode())
        trace=json.loads(raw)
        assert len(trace['decision_trace'])==row['decisions']
        past=root/('past-'+row['scene'])/'persona.db'
        with Store.open(past,readonly=True) as store:
            assert not verify(store)+store.integrity_problems()
            prefix=[e.to_dict() for e in store.events()]
            initial=store.latest_snapshot()
        with Store.open(path.parent/'persona.db',readonly=True) as store:
            assert store.store_kind=='experimental'
            assert not verify(store)+store.integrity_problems()
            ctl=StateController(store)
            events=ctl.events_after(0)
            assert [e.to_dict() for e in events if e.seq<=row['common_past']['prefix_seq']]==prefix
            assert store.get_snapshot(initial.epoch,initial.state_version).state==initial.state
            event_map={e.event_id:e for e in events}
            helpers['verify_intentions'](store,events,event_map)
            normal=Normalizer()
            journal=normal([e.to_dict() for e in events])
            assert trace['journal']==journal
            assert trace['final_state']==normal(ctl.state)
            requests=[e.payload['request'] for e in events if e.type=='proposal.requested']
            wanted={(r['epoch'],r['state_version']) for r in requests}
            memory_states={(r.epoch,r.state_version):r.memory for r in replay(store) if (r.epoch,r.state_version) in wanted}
            # Постановка не вызывает модель: все записанные запросы прошлого scripted.
            new_requests=[r for e in events if e.seq>len(prefix) and e.type=='proposal.requested'
                          for r in [e.payload['request']]]
            assert row['decisions']<=4 and len(new_requests)==row['calls']<=2*row['decisions']
            assert all(1<=len(d['requests'])<=2 for d in trace['decision_trace'])
            for d in trace['decision_trace']:
                assert d['requests'][0]['limits']['attempt']==0 and 'retry_of' not in d['requests'][0]
                if len(d['requests'])==2:
                    assert d['requests'][1]['limits']['attempt']==1
                    assert d['requests'][1]['retry_of']==d['requests'][0]['request_id']
            assert [normal(r) for r in new_requests]==[r for d in trace['decision_trace'] for r in d['requests']]
            for req in requests:
                snapshot=store.get_snapshot(req['epoch'],req['state_version']).state
                counts['recomputed_signal_items']+=check_signals(req,snapshot,memory_states[(req['epoch'],req['state_version'])],event_map)
                payload=json.loads(req['context'][-1]['text'])
                signal=payload['motivation']
                if row['mode']=='off' and req in new_requests:
                    assert signal=={'enabled':False}
                    assert not payload['development']['profile']['motives']
                elif req in new_requests:
                    assert signal['enabled'] is True and signal['mechanism']==row['mode']
                if signal['enabled']:
                    visible={item['ref']:item for item in req['context']}
                    for item in signal['items']:
                        for mid in item['sources']:
                            assert mid in visible and visible[mid]['layer']=='personal'
                            rec=store.get_memory(mid)
                            assert rec.layer=='personal' and rec.origin=='observation'
                            basis=event_map[rec.sources[0]]
                            assert basis.type in ('action.confirmed','action.failed')
                            assert basis.seq<next(e.seq for e in events if e.type=='proposal.requested' and e.payload['request']['request_id']==req['request_id'])
                        for key in item['accepted_goals']:
                            goal=snapshot['intentions'][key]
                            assert goal['status']=='active' and item['activity']=='project:'+goal['project']
                        counts['signal_items']+=1
                counts['requests']+=1
            for event in events:
                if event.seq>len(prefix) and event.type=='proposal.received' and summary['live']:
                    provider=event.payload['response']['provider']
                    assert provider['model']=='gpt-6-luna' and provider['name']=='codex'
                    counts['model_responses']+=1
            world=ctl.state['world_time']['world']['development']
            for project in world['projects'].values():
                for version in project['versions']:
                    artifact=path.parent/'artifacts'/(version['action_id']+'.txt')
                    assert artifact.read_text()==version['text']
                    result=event_map[version['source']]
                    assert result.type=='action.confirmed'
                    assert result.payload['summary']==version['text']
                    counts['artifacts']+=1
            actual=Counter(a['status'] for d in trace['decision_trace'] for a in d['report']['actions'])
            assert dict(actual)==row['action_outcomes']
            counts.update({'cells':1,'choices':row['decisions'],'live_calls':row['calls'] if summary['live'] else 0})
            rows.append({'scene':row['scene'],'mode':row['mode'],'repeat':row['repeat'],
                         'intentions':len(ctl.state['intentions']), 'action_outcomes':dict(actual),
                         'trace_sha256':row['sha256'], 'verified':True})
    return {'protocol':summary['protocol'],'live':summary['live'],'counts':dict(counts),'runs':rows,
            'all_executed':counts['cells']==24,'secrets_found':0}


if __name__=='__main__':
    print(json.dumps(check(sys.argv[1]),ensure_ascii=False,indent=2))
