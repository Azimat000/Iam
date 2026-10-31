"""D-052: независимая read-only сверка сеансов, источников, целей и реальных результатов."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import runpy
import sys

from iam.backup import SecretScanner
from iam.experiments.agency import CONTRACT, PERSONAL
from iam.experiments.conversation import records
from iam.experiments.provenance import INSTRUCTION, REGISTRY
from iam.experiments.results import Normalizer
from iam.experiments.fidelity import VERSION, INSTRUCTION as FIXED_INSTRUCTION
from iam.experiments.transfer import GUIDANCE
from iam.state import StateController, replay, verify
from iam.storage import Store

HERE=Path(__file__).resolve().parent
PREVIOUS=runpy.run_path(str(HERE.parent/'provenance-v1/verify.py'))
SIGNALS=runpy.run_path(str(HERE.parent/'transfer-v1/verify.py'))
ASSESS=runpy.run_path(str(HERE/'assess.py'))['assess']
EXPECTED={(d,1) for d in ('F','N','E','CF','CN')}
AUDIT=runpy.run_path(str(HERE/'audit.py'))
CHECK_SIGNALS=runpy.run_path(str(HERE/'signals.py'))['check_signals']
KINDS=['action','message','intention','memory_write','no_action']




def source_scopes(action,state,ref):
    if action['params'].get('operation')=='write_project':return ['artifact']
    if action['params'].get('operation')=='read_material':return ['received_text']
    if action['params'].get('operation')=='open_project':
        origin=state['world_time']['world'][REGISTRY]['outcomes'].get(ref,{}).get('content_origin')
        return ['received_text','artifact'] if origin in ('model_written_version','scripted_version') else ['received_text']
    return []

def verify_goals(store,events,event_map,prefix):
    proposals={p['proposal_id']:p for e in events if e.type=='proposal.received' for p in e.payload['response']['proposals']}
    requests={e.payload['request']['request_id']:e for e in events if e.type=='proposal.requested'}
    original={}
    for e in events:
        if e.type!='proposal.accepted' or e.payload['kind']!='intention':continue
        p=proposals[e.payload['proposal_id']];body=p['body'];q=requests[e.payload['request_id']]
        state=store.get_snapshot(e.epoch,e.state_version).state;goal=state['intentions'][body['id']]
        if body['op']=='create':
            fields={'op','id','text','operation','target','criterion'}
            assert set(body)==(fields|{'evidence_kind'} if e.seq>prefix else fields)
            assert goal['project']==body['target'] and goal['text']==body['text']
            assert goal.get('evidence_kind')==body.get('evidence_kind')
            assert goal['operation']==body['operation'] and goal['criterion']==body['criterion'] and goal['criterion'].strip()
            assert q.seq<=goal['created_seq']<e.seq and goal['sources'] and set(goal['sources'])<=set(p['cites'])
            original[body['id']]={k:goal.get(k) for k in ('text','project','operation','criterion','evidence_kind')}
        elif body['to_status']=='done' and e.seq>prefix:
            result=event_map[body['result_ref']];action=state['world_time']['world']['actions'][result.payload['action_id']]
            assert result.type=='action.confirmed' and goal['created_seq']<result.seq<e.seq
            assert action['params']['operation']==goal['operation'] and action['params']['target']==goal['project']
            quote=body['result_quote'];assert quote and quote in action['outcome'] and goal['result_quote']==quote
            assert result.event_id in p['cites'] and result.event_id in {item['ref'] for item in q.payload['request']['context']}
            assert any(quote in item['text'] for item in q.payload['request']['context'] if item['ref']==result.event_id)
            assert goal.get('evidence_kind') is None or goal['evidence_kind'] in source_scopes(action,state,body['result_ref'])
            assert goal['semantic_verified'] is False and goal['status']=='done' and goal['result_ref']==result.event_id
            saved=goal['completion_assessment'];audit=event_map[saved['event']]
            assert audit.type=='proposal.assessed' and q.seq<audit.seq<e.seq
            assert saved['binding']==audit.payload['binding'] and saved['kind']=='model_assessed'
            assert saved['provider']==audit.payload['evaluation']['response']['provider']
            assert saved['requirement']==audit.payload['requirement']['assessment']
            assert {k:saved[k] for k in audit.payload['assessment']}==audit.payload['assessment']
            assert saved['verdict']=='pass' and all(saved[k] is True for k in ('goal_met','criterion_met','evidence_sufficient'))
        assert {k:goal.get(k) for k in ('text','project','operation','criterion','evidence_kind')}==original[body['id']]
        for key,g in state['intentions'].items():assert {k:g.get(k) for k in ('text','project','operation','criterion','evidence_kind')}==original[key]

def check(root,require_final=True):
    root=Path(root);summary=json.loads((root/'summary.json').read_text())
    assert summary['protocol']=='fidelity-v2' and summary['profile_version']==VERSION
    assert summary['planned_decisions']==sum(summary['session_counts'].values())*summary['choices_per_session']
    expected={(k,1) for k in summary['sources']};assert expected<=EXPECTED
    if summary['live']:
        assert expected==EXPECTED
        assert summary['session_counts']=={'F':6,'N':6,'E':6,'CF':3,'CN':3}
        assert summary['choices_per_session']==2 and summary['planned_decisions']==48
        order=[('F',1),('N',1),('E',1),('CF',1),('CN',1)]
        assert [(r['history'],r['repeat']) for r in summary['runs']]==(order if require_final else order[:len(summary['runs'])])
    assert all(summary['conditions'][k]==0 for k in ('goal_questions','reminders','incoming_messages','seeded_intentions'))
    assert summary['conditions']['instruction_sha256']=={k:hashlib.sha256(v.encode()).hexdigest() for k,v in {'fixed':FIXED_INSTRUCTION}.items()}
    keys=[(r['history'],r['repeat']) for r in summary['runs']];assert len(keys)==len(set(keys)) and set(keys)<=expected
    if require_final:assert set(keys)==expected
    counts=Counter();results=[];fingerprints={}
    for row in summary['runs']:
        if row['status']=='not_run':counts['not_run']+=1;continue
        if row['status']=='running':
            assert not require_final;counts['running']+=1;continue
        assert row['profile']=='fixed'
        instruction=FIXED_INSTRUCTION
        source=summary['sources'][row['history']];directory=Path(source['directory'])
        source_raw=(directory/'trace.json').read_bytes();assert hashlib.sha256(source_raw).hexdigest()==source['sha256']
        with Store.open(directory/'persona.db',readonly=True) as store:
            assert not verify(store)+store.integrity_problems()
            original=[e.to_dict() for e in store.events()];initial=store.latest_snapshot()
            assert len(initial.state['intentions'])==source['initial_intentions']
            if source['source_kind']=='prepared_no_goals':assert not initial.state['intentions']
            else:
                assert source['source_kind']=='natural_live_checkpoint'
                responses={p['proposal_id']:e['payload']['response'] for e in original if e['type']=='proposal.received' for p in e['payload']['response']['proposals']}
                assert all(responses[g['proposal_id']]['provider']=={'name':'codex','model':'gpt-6-luna'} for g in initial.state['intentions'].values())
            assert not any(e['type'] in ('input.arrived','creator.reply','operator.intervention') for e in original)
            assert all(e['payload']['request']['context'][0]['text'] in {PERSONAL+CONTRACT+GUIDANCE, __import__('iam.experiments.commitment',fromlist=['INSTRUCTION']).INSTRUCTION}
                       for e in original if e['type']=='proposal.requested')
        assert row['initial']==source
        if row['history'] in fingerprints:assert fingerprints[row['history']]==row['initial']
        fingerprints[row['history']]=row['initial']
        path=root/row['trace'];raw=path.read_bytes();assert hashlib.sha256(raw).hexdigest()==row['sha256']
        assert not SecretScanner(assignments=False).find(raw.decode())
        trace=json.loads(raw);assert len(trace['decision_trace'])==row['decisions']
        with Store.open(path.parent/'persona.db',readonly=True) as store:
            assert store.store_kind=='experimental' and not verify(store)+store.integrity_problems()
            ctl=StateController(store);events=ctl.events_after(0);event_map={e.event_id:e for e in events}
            assert [e.to_dict() for e in events if e.seq<=source['prefix_seq']]==original
            assert store.get_snapshot(initial.epoch,initial.state_version).state==initial.state
            normal=Normalizer();assert trace['journal']==normal([e.to_dict() for e in events]);assert trace['final_state']==normal(ctl.state)
            new=[e for e in events if e.seq>source['prefix_seq']]
            assert not any(e.type in ('input.arrived','creator.reply','operator.intervention') for e in new)
            starts=[e for e in new if e.type=='session.started'];stops=[e for e in new if e.type=='session.stopped']
            assert len(starts)==len(stops)==row['sessions']==len(trace['sessions'])
            assert len({e.payload['session_id'] for e in starts})==len(starts)
            old_state=initial.state;old_life=source['life_ms']
            requests=[e for e in new if e.type=='proposal.requested']
            assert len(requests)==row['choice_calls']<=row['decisions']*2
            audits=[e for e in new if e.type=='proposal.assessed']
            assert sum(stage['attempted'] for e in audits for stage in (e.payload['requirement'],e.payload['evaluation']) if stage)==row['review_calls']
            assert row['calls']==row['choice_calls']+row['review_calls']
            counts['checked_assessments']+=AUDIT['check'](audits,events,store,source_scopes,summary['live'])
            pairs=[]
            for request in requests:
                accepted=[e for e in new if e.type=='proposal.accepted' and e.payload['request_id']==request.payload['request']['request_id']]
                kinds=[e.payload['kind'] for e in accepted]
                assert kinds.count('memory_write')<=2
                primary=[k for k in kinds if k!='memory_write']
                if row['profile']=='control':assert len(primary)<=1
                else:
                    assert kinds.count('intention')<=1 and sum(k in ('action','message') for k in kinds)<=1
                    assert kinds.count('no_action')<=1 and ('no_action' not in kinds or len(primary)==1)
                if 'intention' in kinds and any(k in ('action','message') for k in kinds):
                    pairs.append({'request_id':request.payload['request']['request_id'],'accepted_order':primary,
                                  'events':[e.event_id for e in accepted if e.payload['kind']!='memory_write']})
            counts['accepted_goal_action_pairs']+=len(pairs)
            wanted={(e.epoch,e.payload['request']['state_version']) for e in requests}
            memories={(r.epoch,r.state_version):r.memory for r in replay(store) if (r.epoch,r.state_version) in wanted}
            for number,(start,stop,saved) in enumerate(zip(starts,stops,trace['sessions'])):
                assert start.seq<stop.seq and start.life_time_ms==old_life
                before=store.get_snapshot(start.epoch,start.state_version-1).state
                assert before==old_state
                started=store.get_snapshot(start.epoch,start.state_version).state
                assert {k:v for k,v in started.items() if k!='world_time'}=={k:v for k,v in before.items() if k!='world_time'}
                assert started['world_time']['world']==before['world_time']['world']
                snapshot=store.get_snapshot(stop.epoch,stop.state_version)
                assert saved['seq']==stop.seq and saved['session']==number and saved['restored'] is True
                assert saved['state']==normal(snapshot.state) and saved['life_ms']==stop.life_time_ms
                choices=[d for d in trace['decision_trace'] if d['session']==number]
                assert 1<=len(choices)<=summary['choices_per_session']
                assert stop.life_time_ms-start.life_time_ms==60000*len(choices)
                assert sum(start.seq<e.seq<stop.seq for e in requests)+sum(start.seq<e.seq<stop.seq and sum(stage['attempted'] for stage in (e.payload['requirement'],e.payload['evaluation']) if stage) for e in audits)<=10
                old_state,old_life=snapshot.state,stop.life_time_ms
                counts['restored_sessions']+=1
            assert [normal(e.payload['request']) for e in requests]==[q for d in trace['decision_trace'] for q in d['requests']]
            for e in requests:
                q=e.payload['request'];assert q['context'][0]['text']==instruction
                assert q['purpose']=='choose_action' and q['allowed_proposals']==KINDS
                assert q['limits']['max_output_tokens']==1800 and q['limits']['deadline_ms']==60000
                state=store.get_snapshot(q['epoch'],q['state_version']).state
                memory=memories[(q['epoch'],q['state_version'])];view=json.loads(q['context'][-1]['text'])
                assert view['development']['intentions']=={key:{'text':item['text'],'status':item['status'],'operation':item.get('operation','write_project'),'target':item['project'],'criterion':item.get('criterion'),'criterion_recorded':'criterion' in item,'evidence_kind':item.get('evidence_kind'),'semantic_verified':False,'completion_assessment':item.get('completion_assessment','historical_unassessed' if item['status']=='done' else None)} for key,item in state['intentions'].items()}
                cutoff=state['world_time']['world'][REGISTRY]['audit']['setup_seq'] if state['world_time']['world'][REGISTRY]['audit'] else 0
                computed=PREVIOUS['classifications']([ev for ev in events if ev.seq<e.seq],state,cutoff)
                assert state['world_time']['world'][REGISTRY]['outcomes']==computed
                byref={c['ref']:c for c in q['context']};tags=view['experience_provenance']
                for ref,item in tags.items():
                    basis=memory[ref]['sources'][0] if ref in memory else ref
                    assert item==computed[basis]
                    if item['selection']!='model_live':assert json.loads(q['context'][-2]['text'])['experience_sources'][ref]['self_quote']==''
                    counts['checked_provenance_refs']+=1
                assert view['development']['result_scopes']=={'write_project':['artifact'],'read_material':['received_text'],'open_project':['received_text','artifact_if_own_version'],'independent_observation_available':False}
                for ref,meta in json.loads(q['context'][-2]['text'])['experience_sources'].items():
                    basis=memory[ref]['sources'][0] if ref in memory else ref
                    action=state['world_time']['world']['actions'].get(event_map[basis].payload.get('action_id')) if basis in event_map else None
                    assert meta['result_scopes']==(source_scopes(action,state,basis) if meta['operation'] else [])
                counts['recomputed_signal_items']+=CHECK_SIGNALS(q,state,memory,event_map)
            for e in new:
                if e.type=='proposal.received' and summary['live']:
                    assert e.payload['response']['provider']=={'name':'codex','model':'gpt-6-luna'};counts['model_responses']+=1
            verify_goals(store,events,event_map,source['prefix_seq'])
            for record in records(ctl):
                if record.origin!='own_hypothesis':continue
                c=record.content
                accepted=next(e for e in events if e.event_id==record.sources[1])
                request=next(e.payload['request'] for e in events if e.type=='proposal.requested'
                             and e.payload['request']['request_id']==accepted.payload['request_id'])
                shown=next(item['text'] for item in request['context'] if item['ref']==c['source_ref'])
                assert c['quote'] in shown
            for project in ctl.state['world_time']['world']['development']['projects'].values():
                for v in project['versions']:
                    assert (path.parent/'artifacts'/(v['action_id']+'.txt')).read_text()==v['text']
                    assert event_map[v['source']].type=='action.confirmed';counts['artifacts']+=1
            for d in trace['decision_trace']:
                assert 1<=len(d['requests'])<=2 and d['requests'][0]['limits']['attempt']==0
                if len(d['requests'])==2:assert d['requests'][1]['retry_of']==d['requests'][0]['request_id'] and d['requests'][1]['limits']['attempt']==1
            if row['status']=='completed':
                assert row['sessions']==summary['session_counts'][row['history']] and row['decisions']==summary['session_counts'][row['history']]*summary['choices_per_session']
            else:assert row['status']=='technical_pause' and trace['status']=='technical_pause'
            analysis=ASSESS(trace,source['prefix_seq'],set(initial.state['world_time']['world']['development']['projects']))
            results.append({'code':row['code'],'profile':row['profile'],'sha256':row['sha256'],'verified':True,
                            'coaccepted_pairs':pairs,'assessment':analysis})
            counts['runs']+=1;counts['decisions']+=row['decisions'];counts['calls']+=row['calls']
    return {'protocol':summary['protocol'],'live':summary['live'],'counts':dict(counts),'runs':results,
            'all_completed':len(results)==len(expected) and all(r['status']=='completed' for r in summary['runs']),
            'no_additional_goal_questions_or_external_reminders':True,'technical_intention_contract_present':True,'human_ratings':None,'secrets_found':0}


if __name__=='__main__':print(json.dumps(check(sys.argv[1]),ensure_ascii=False,indent=2))
