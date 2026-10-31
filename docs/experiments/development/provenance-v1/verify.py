"""D-045: независимая сверка источников по исходным базам, не по заявлениям модели."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import runpy
import sys

from iam.backup import SecretScanner
from iam.experiments.results import Normalizer
from iam.state import StateController, replay, verify
from iam.storage import Store

ROOT=Path(__file__).resolve().parents[4]
PREVIOUS=runpy.run_path(str(ROOT/'docs/experiments/development/transfer-v1/verify.py'))
TYPES={'action.confirmed','action.failed','action.unknown_outcome','message.out_sent','message.out_unknown'}


def classifications(events,state,boundary):
    providers={e.payload['response']['request_id']:e.payload['response']['provider'] for e in events if e.type=='proposal.received'}
    accepted={e.payload['proposal_id']:e.payload['request_id'] for e in events if e.type=='proposal.accepted'}
    result={};writers={}
    for e in events:
        if e.type not in TYPES:continue
        action=state['world_time']['world']['actions'][e.payload['action_id']]
        p=providers.get(accepted.get(e.payload['proposal_id']),{})
        chosen=('prepared_setup' if e.seq<=boundary else 'model_live' if p=={'name':'codex','model':'gpt-6-luna'}
                else 'scripted_choice' if p.get('name')=='scripted' else 'unknown')
        if chosen=='prepared_setup':assert p.get('name')=='scripted'
        operation=action['params'].get('operation','message');target=action['params'].get('target','')
        writer=chosen if operation=='write_project' else writers.get(target,'unknown')
        origin='none'
        if action['status']=='confirmed':
            if operation in ('open_project','write_project'):
                origin={'prepared_setup':'prepared_version','model_live':'model_written_version','scripted_choice':'scripted_version'}.get(writer,'unknown_version')
            else:origin={'read_material':'supplied_material','read_message':'external_statement'}.get(operation,'none')
        result[e.event_id]={'selection':chosen,'operation':operation,'target':target,'result_status':action['status'],'content_origin':origin}
        if operation=='write_project' and action['status']=='confirmed':writers[target]=chosen
    return result


def diagnose(decision,expected,normal):
    proposals=[p for pack in decision['proposals'] for p in pack['response']['proposals'] if p['kind']=='no_action']
    answer=None
    if proposals:
        try:answer=json.loads(proposals[-1]['body']['reason'])
        except (ValueError,TypeError):pass
    counts=Counter();found={}
    if isinstance(answer,dict) and isinstance(answer.get('versions'),list):
        for item in answer['versions']:
            if isinstance(item,dict) and isinstance(item.get('source'),str):found[item['source']]=item.get('selection')
    for ref,actual in expected.items():
        selected=found.get(ref)
        counts['correct' if selected==actual else 'unknown' if selected=='unknown' else 'omitted' if selected is None else 'misattributed']+=1
    return {'expected':{normal.names.get(ref,ref):who for ref,who in expected.items()},
            'classification':dict(counts),'answer':answer,
            'opening_is_not_writing':isinstance(answer,dict) and answer.get('opening_creates_text') is False,
            'fiction_is_not_biography':isinstance(answer,dict) and answer.get('fiction_is_biography') is False,
            'format_readable':isinstance(answer,dict),'unexpected_sources':len(set(found)-set(expected))}


def check(root):
    root=Path(root);summary=json.loads((root/'summary.json').read_text())
    expected_codes={'diagnosis-legacy-1','diagnosis-legacy-2','diagnosis-fixed-1','diagnosis-fixed-2','free-fixed-1','free-fixed-2'}
    assert len(summary['runs'])==6 and {r['code'] for r in summary['runs']}==expected_codes
    with Store.open(Path(summary['source'])/'persona.db',readonly=True) as store:
        assert not verify(store)+store.integrity_problems()
        prefix=[e.to_dict() for e in store.events()];initial=store.latest_snapshot()
    results=[];counts=Counter()
    for row in summary['runs']:
        if row['status']!='executed':counts['not_run']+=1;continue
        path=root/row['trace'];raw=path.read_bytes()
        assert hashlib.sha256(raw).hexdigest()==row['sha256'] and not SecretScanner(assignments=False).find(raw.decode())
        trace=json.loads(raw);assert len(trace['decision_trace'])==row['decisions']
        with Store.open(path.parent/'persona.db',readonly=True) as store:
            assert store.store_kind=='experimental' and not verify(store)+store.integrity_problems()
            ctl=StateController(store);events=ctl.events_after(0);event_map={e.event_id:e for e in events}
            assert [e.to_dict() for e in events if e.seq<=len(prefix)]==prefix
            assert store.get_snapshot(initial.epoch,initial.state_version).state==initial.state
            normal=Normalizer();assert trace['journal']==normal([e.to_dict() for e in events]);assert trace['final_state']==normal(ctl.state)
            assert ctl.state['world_time']['world']['experiment_provenance']['outcomes']==classifications(events,ctl.state,summary['setup_seq'])
            requests=[e for e in events if e.type=='proposal.requested' and e.seq>len(prefix)]
            assert len(requests)==row['calls']<=2*row['decisions']
            assert [normal(e.payload['request']) for e in requests]==[q for d in trace['decision_trace'] for q in d['requests']]
            wanted={(e.epoch,e.payload['request']['state_version']) for e in requests}
            memories={(r.epoch,r.state_version):r.memory for r in replay(store) if (r.epoch,r.state_version) in wanted}
            expected_versions={}
            for e in requests:
                req=e.payload['request'];state=store.get_snapshot(req['epoch'],req['state_version']).state
                memory=memories[(req['epoch'],req['state_version'])];value=json.loads(req['context'][-1]['text'])
                computed=classifications([ev for ev in events if ev.seq<e.seq],state,summary['setup_seq'])
                assert state['world_time']['world']['experiment_provenance']['outcomes']==computed
                counts['recomputed_signal_items']+=PREVIOUS['check_signals'](req,state,memory,event_map)
                visible={c['ref']:memory[c['ref']] for c in req['context'] if c['ref'] in memory and c['layer']=='personal' and memory[c['ref']]['origin']=='observation'}
                for ref,rec in visible.items():
                    basis=rec['sources'][0]
                    if basis in computed and computed[basis]['operation']=='write_project':expected_versions[ref]=computed[basis]['selection']
                if row['profile']=='fixed':
                    tags=value['experience_provenance'];byref={c['ref']:c for c in req['context']}
                    assert set(visible)<=set(tags)
                    for ref,item in tags.items():
                        basis=memory[ref]['sources'][0] if ref in memory else ref
                        assert item==computed[basis]
                        action=state['world_time']['world']['actions'][event_map[basis].payload['action_id']]
                        if action['status']=='confirmed' and action['kind']=='action':
                            assert byref[ref]['text'].split('\n',2)[-1]==action['outcome'], 'изменён текст результата'
                        source_info=json.loads(req['context'][-2]['text'])['experience_sources'][ref]
                        if item['selection']!='model_live':assert source_info['self_quote']==''
                        if item['selection']=='prepared_setup':assert byref[ref]['text'].startswith('Подготовлено стендом:')
                        counts['checked_provenance_refs']+=1
                    for key,p in state['world_time']['world']['development']['projects'].items():
                        refs={rec['sources'][0]:ref for ref,rec in visible.items()}
                        versions=[v for v in p['versions'] if v['source'] in refs]
                        info=value['development']['work_continuity'][key]
                        assert info['visible_versions']==len(versions) and info['sources']==[refs[v['source']] for v in versions]
                        expected_same=versions[-1]['text']==versions[-2]['text'] if len(versions)>=2 else None
                        assert info['last_two_visible_texts_identical']==expected_same
                else:assert 'experience_provenance' not in value
                if row['task']=='diagnosis':assert req['allowed_proposals']==['no_action'] and summary['question'] in req['context'][0]['text']
                counts['requests']+=1
            for e in events:
                if e.seq>len(prefix) and e.type=='proposal.received' and summary['live']:
                    assert e.payload['response']['provider']=={'name':'codex','model':'gpt-6-luna'};counts['model_responses']+=1
            for p in ctl.state['world_time']['world']['development']['projects'].values():
                for v in p['versions']:
                    assert (path.parent/'artifacts'/(v['action_id']+'.txt')).read_text()==v['text'];counts['artifacts']+=1
            decisions=[]
            for d in trace['decision_trace']:
                assert 1<=len(d['requests'])<=2 and d['requests'][0]['limits']['attempt']==0
                if len(d['requests'])==2:assert d['requests'][1]['retry_of']==d['requests'][0]['request_id'] and d['requests'][1]['limits']['attempt']==1
            if row['task']=='diagnosis':
                # reason сохранён дословно; ссылки в нём исходные, поэтому оценка по сырому ответу базы.
                received=[e.payload for e in events if e.type=='proposal.received' and e.seq>len(prefix)]
                score=diagnose({'proposals':received},expected_versions,normal)
            else:score=None
            results.append({'code':row['code'],'sha256':row['sha256'],'verified':True,'diagnosis':score})
            counts['cells']+=1;counts['decisions']+=row['decisions'];counts['calls']+=row['calls']
    assert all(r['initial']==summary['runs'][0]['initial'] for r in summary['runs'] if r['status']=='executed')
    return {'protocol':summary['protocol'],'live':summary['live'],'counts':dict(counts),'runs':results,'secrets_found':0,'human_ratings':None}


if __name__=='__main__':print(json.dumps(check(sys.argv[1]),ensure_ascii=False,indent=2))
