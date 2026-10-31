"""D-043: read-only сверка общего живого прошлого, цели и шести ветвей."""
import hashlib
import json
from pathlib import Path
import runpy
import sys

from iam.backup import SecretScanner
from iam.experiments.results import Normalizer
from iam.state import StateController, replay, verify
from iam.storage import Store

HERE=Path(__file__).resolve().parent
V=runpy.run_path(str(HERE/'verify.py'))


def check(root):
    root=Path(root);s=json.loads((root/'summary.json').read_text());rows=s['runs']
    assert rows[0]['code']=='common-goal' and rows[0]['question'] and rows[0]['decisions']==1
    assert s['question_elicited_not_spontaneous'] is True
    expected={'common-goal'}|({f'{m}-{r}' for m in ('needs','goals','off') for r in (1,2)} if s['goal_accepted'] and not s['provider_paused'] else set())
    assert len(rows)==len({r['code'] for r in rows})
    if not s['provider_paused']:assert {r['code'] for r in rows}==expected
    result=[];calls=0;signals=0;model_responses=0
    for row in rows:
        path=root/row['trace'];raw=path.read_bytes()
        assert hashlib.sha256(raw).hexdigest()==row['sha256'] and not SecretScanner(assignments=False).find(raw.decode())
        t=json.loads(raw);assert len(t['decision_trace'])==row['decisions']
        source=Path(s['source']) if row['question'] else root/'common-goal'
        with Store.open(source/'persona.db',readonly=True) as store:
            prefix=[e.to_dict() for e in store.events()];state=store.latest_snapshot()
            assert not verify(store)+store.integrity_problems()
        with Store.open(path.parent/'persona.db',readonly=True) as store:
            assert not verify(store)+store.integrity_problems()
            ctl=StateController(store);events=ctl.events_after(0);event_map={e.event_id:e for e in events}
            assert [e.to_dict() for e in events if e.seq<=row['initial']['prefix_seq']]==prefix
            assert store.get_snapshot(state.epoch,state.state_version).state==state.state
            V['helpers']['verify_intentions'](store,events,event_map)
            normal=Normalizer();assert t['journal']==normal([e.to_dict() for e in events]);assert t['final_state']==normal(ctl.state)
            reqs=[e.payload['request'] for e in events if e.type=='proposal.requested' and e.seq>len(prefix)]
            assert len(reqs)==row['calls']<=row['decisions']*2
            wanted={(r['epoch'],r['state_version']) for r in reqs}
            memories={(r.epoch,r.state_version):r.memory for r in replay(store) if (r.epoch,r.state_version) in wanted}
            assert [normal(r) for r in reqs]==[r for d in t['decision_trace'] for r in d['requests']]
            for req in reqs:
                if row['question']:
                    assert req['allowed_proposals']==['intention','no_action'] and s['question'] in req['context'][0]['text']
                snapshot=store.get_snapshot(req['epoch'],req['state_version']).state
                signals+=V['check_signals'](req,snapshot,memories[(req['epoch'],req['state_version'])],event_map)
                sig=json.loads(req['context'][-1]['text'])['motivation']
                assert sig['enabled']==(row['mode']!='off')
                if sig['enabled']:assert sig['mechanism']==row['mode']
            for d in t['decision_trace']:
                assert 1<=len(d['requests'])<=2 and d['requests'][0]['limits']['attempt']==0
                if len(d['requests'])==2:assert d['requests'][1]['retry_of']==d['requests'][0]['request_id'] and d['requests'][1]['limits']['attempt']==1
            for event in events:
                if event.seq>len(prefix) and event.type=='proposal.received' and s['live']:
                    assert event.payload['response']['provider']=={'name':'codex','model':'gpt-6-luna'}
                    model_responses+=1
            for project in ctl.state['world_time']['world']['development']['projects'].values():
                for version in project['versions']:
                    assert (path.parent/'artifacts'/(version['action_id']+'.txt')).read_text()==version['text']
                    assert event_map[version['source']].type=='action.confirmed'
            result.append({'code':row['code'],'sha256':row['sha256'],'verified':True,'intentions':t['final_state']['intentions']})
            calls+=row['calls']
    if len(rows)>2:assert all(row['initial']==rows[1]['initial'] for row in rows[1:])
    return {'protocol':s['protocol'],'live':s['live'],'cells':len(rows),'calls':calls,'recomputed_signal_items':signals,
            'model_responses':model_responses,'runs':result,'question_elicited_not_spontaneous':True,'secrets_found':0}


if __name__=='__main__':
    print(json.dumps(check(sys.argv[1]),ensure_ascii=False,indent=2))
