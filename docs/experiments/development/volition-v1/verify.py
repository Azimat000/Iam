"""D-048: независимая read-only сверка сеансов, источников, целей и реальных результатов."""
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
from iam.experiments.volition import VERSION, INSTRUCTION as FIXED_INSTRUCTION
from iam.experiments.transfer import GUIDANCE
from iam.state import StateController, replay, verify
from iam.storage import Store

HERE=Path(__file__).resolve().parent
PREVIOUS=runpy.run_path(str(HERE.parent/'provenance-v1/verify.py'))
SIGNALS=runpy.run_path(str(HERE.parent/'transfer-v1/verify.py'))
ASSESS=runpy.run_path(str(HERE.parent/'autonomy-v1/assess.py'))['assess']
EXPECTED={('H1',1),('H1',2),('H2',1),('H2',2)}
KINDS=['action','message','intention','memory_write','no_action']


def check(root,require_final=True):
    root=Path(root);summary=json.loads((root/'summary.json').read_text())
    assert summary['protocol']=='volition-v1' and summary['profile_version']==VERSION
    assert summary['planned_decisions']==4*summary['sessions_per_run']*summary['choices_per_session']
    assert all(summary['conditions'][k]==0 for k in ('goal_questions','reminders','incoming_messages','seeded_intentions'))
    assert summary['conditions']['instruction_sha256']=={k:hashlib.sha256(v.encode()).hexdigest() for k,v in {'control':INSTRUCTION,'fixed':FIXED_INSTRUCTION}.items()}
    keys=[(r['history'],r['repeat']) for r in summary['runs']];assert len(keys)==len(set(keys)) and set(keys)<=EXPECTED
    if require_final:assert set(keys)==EXPECTED
    counts=Counter();results=[];fingerprints={}
    for row in summary['runs']:
        if row['status']=='not_run':counts['not_run']+=1;continue
        if row['status']=='running':
            assert not require_final;counts['running']+=1;continue
        assert row['profile']==('control' if row['repeat']==1 else 'fixed')
        instruction=INSTRUCTION if row['profile']=='control' else FIXED_INSTRUCTION
        source=summary['sources'][row['history']];directory=Path(source['directory'])
        source_raw=(directory/'trace.json').read_bytes();assert hashlib.sha256(source_raw).hexdigest()==source['sha256']
        with Store.open(directory/'persona.db',readonly=True) as store:
            assert not verify(store)+store.integrity_problems()
            original=[e.to_dict() for e in store.events()];initial=store.latest_snapshot()
            assert not initial.state['intentions']
            assert not any(e['type'] in ('input.arrived','creator.reply') for e in original)
            assert all(e['payload']['request']['context'][0]['text'] in {INSTRUCTION,PERSONAL+CONTRACT+GUIDANCE}
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
            assert len(requests)==row['calls']<=row['decisions']*2
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
                assert sum(start.seq<e.seq<stop.seq for e in requests)<=10
                old_state,old_life=snapshot.state,stop.life_time_ms
                counts['restored_sessions']+=1
            assert [normal(e.payload['request']) for e in requests]==[q for d in trace['decision_trace'] for q in d['requests']]
            for e in requests:
                q=e.payload['request'];assert q['context'][0]['text']==instruction
                assert q['purpose']=='choose_action' and q['allowed_proposals']==KINDS
                assert q['limits']['max_output_tokens']==1800 and q['limits']['deadline_ms']==60000
                state=store.get_snapshot(q['epoch'],q['state_version']).state
                memory=memories[(q['epoch'],q['state_version'])];view=json.loads(q['context'][-1]['text'])
                assert view['development']['intentions']=={key:{k:item[k] for k in ('text','project','status')}
                                                         for key,item in state['intentions'].items()}
                cutoff=state['world_time']['world'][REGISTRY]['audit']['setup_seq'] if state['world_time']['world'][REGISTRY]['audit'] else 0
                computed=PREVIOUS['classifications']([ev for ev in events if ev.seq<e.seq],state,cutoff)
                assert state['world_time']['world'][REGISTRY]['outcomes']==computed
                byref={c['ref']:c for c in q['context']};tags=view['experience_provenance']
                for ref,item in tags.items():
                    basis=memory[ref]['sources'][0] if ref in memory else ref
                    assert item==computed[basis]
                    if item['selection']!='model_live':assert json.loads(q['context'][-2]['text'])['experience_sources'][ref]['self_quote']==''
                    counts['checked_provenance_refs']+=1
                counts['recomputed_signal_items']+=SIGNALS['check_signals'](q,state,memory,event_map)
            for e in new:
                if e.type=='proposal.received' and summary['live']:
                    assert e.payload['response']['provider']=={'name':'codex','model':'gpt-6-luna'};counts['model_responses']+=1
            SIGNALS['helpers']['verify_intentions'](store,events,event_map)
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
                assert row['sessions']==summary['sessions_per_run'] and row['decisions']==summary['sessions_per_run']*summary['choices_per_session']
            else:assert row['status']=='technical_pause' and trace['status']=='technical_pause'
            analysis=ASSESS(trace,source['prefix_seq'],set(initial.state['world_time']['world']['development']['projects']))
            results.append({'code':row['code'],'sha256':row['sha256'],'verified':True,'assessment':analysis})
            counts['runs']+=1;counts['decisions']+=row['decisions'];counts['calls']+=row['calls']
    return {'protocol':summary['protocol'],'live':summary['live'],'counts':dict(counts),'runs':results,
            'all_completed':len(results)==4 and all(r['status']=='completed' for r in summary['runs']),
            'no_additional_goal_questions_or_external_reminders':True,'technical_intention_contract_present':True,'human_ratings':None,'secrets_found':0}


if __name__=='__main__':print(json.dumps(check(sys.argv[1]),ensure_ascii=False,indent=2))
