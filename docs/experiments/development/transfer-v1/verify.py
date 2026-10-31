"""Независимая сверка исходных баз, запросов и работ серии D-040 (read-only).

PYTHONPATH=src .venv/bin/python docs/experiments/development/transfer-v1/verify.py /каталог/серии
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import runpy
import sys

from iam.backup import SecretScanner
from iam.experiments.results import Normalizer
from iam.state import StateController, verify
from iam.storage import Store

ROOT=Path(__file__).resolve().parents[4]
helpers=runpy.run_path(str(ROOT/'scripts/experiments/verify_development.py'))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def check(directory):
    root=Path(directory)
    summary=json.loads((root/'summary.json').read_text())
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
            # Постановка не вызывает модель: все записанные запросы прошлого scripted.
            new_requests=[r for e in events if e.seq>len(prefix) and e.type=='proposal.requested'
                          for r in [e.payload['request']]]
            assert len(new_requests)==row['calls']==row['decisions']<=4
            assert [normal(r) for r in new_requests]==[r for d in trace['decision_trace'] for r in d['requests']]
            for req in requests:
                snapshot=store.get_snapshot(req['epoch'],req['state_version']).state
                payload=json.loads(req['context'][-1]['text'])
                signal=payload['motivation']
                if row['mode']=='off' and req in new_requests:
                    assert signal=={'enabled':False}
                    assert not payload['development']['profile']['motives']
                elif signal['enabled']:
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
