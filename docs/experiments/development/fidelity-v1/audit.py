"""Независимая сверка квитанций: не вызывает FidelityCycle/parse_review."""
import hashlib
import json
from iam.domain import jsonschema
from iam.experiments.fidelity import REVIEW


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def check(audits,events,store,scopes,live):
    proposals={p['proposal_id']:(p,e.payload['response']) for e in events if e.type=='proposal.received' for p in e.payload['response']['proposals']}
    requests={e.payload['request']['request_id']:e for e in events if e.type=='proposal.requested'}
    by_id={e.event_id:e for e in events}
    for audit in audits:
        payload=audit.payload;query=payload['request'];pr,actor=proposals[payload['proposal_id']]
        event=requests[actor['request_id']];q=event.payload['request'];state=store.get_snapshot(q['epoch'],q['state_version']).state
        assert pr['kind']=='intention' and pr['body']['to_status']=='done'
        packet=json.loads(query['context'][1]['text']);ref=pr['body']['result_ref'];result=by_id[ref]
        action=state['world_time']['world']['actions'][result.payload['action_id']]
        shown=next(c['text'] for c in q['context'] if c['ref']==ref)
        meta=json.loads(q['context'][-2]['text'])['experience_sources'][ref]
        assert result.type=='action.confirmed' and ref in pr['cites']
        assert packet=={'goal':state['intentions'][pr['body']['id']], 'visible_context':q['context'][1:],
            'available_result':{'text':shown,'base':ref,'social':meta['social'],'operation':meta['operation'],
                'self_quote':meta['self_quote'],'scopes':scopes(action,state,ref),'outcome':action['outcome'],'action_id':result.payload['action_id']}}
        assert packet['available_result']['outcome'] in shown
        assert payload['binding']==digest({'request':q,'proposal':pr,'packet':packet,'epoch':q['epoch'],'state_version':q['state_version']})
        assert len(query['context'])==2
        assert query['context'][0]['text']==REVIEW and query['context'][0]['origin']=='model_knowledge' and query['context'][0]['layer']=='state'
        assert query['context'][1]['origin']=='observation' and query['context'][1]['layer']=='state'
        assert query['limits']['max_output_tokens']==q['limits']['max_output_tokens']
        assert query['request_id']!=q['request_id'] and query['idempotency_key']=='req:'+query['request_id']
        assert 'retry_of' not in query and query['limits']['attempt']==0
        assert 0<query['limits']['deadline_ms']<=q['limits']['deadline_ms']
        assert query['allowed_proposals']==['no_action'] and query['allowed_actions']==[] and query['purpose']=='appraise'
        assert query['epoch']==q['epoch'] and query['state_version']==q['state_version']
        assert not audit.changes_state and payload['phase']=='completion_assessment'
        answer=payload['response'];value=payload['assessment']
        if not payload['attempted']:assert answer is None and value is None and payload['error']=='assessment_deadline'
        if value is None:continue
        assert not jsonschema.errors('response',answer)
        assert payload['error'] is None and answer['status']=='ok' and answer['tool_use_detected'] is False
        assert all(answer[k]==query[k] for k in ('request_id','epoch','state_version'))
        assert answer['provider']==actor['provider']
        if live:assert answer['provider']=={'name':'codex','model':'gpt-6-luna'}
        assert len(answer['proposals'])==1
        item=answer['proposals'][0];assert item['kind']=='no_action' and item['cites']==[query['context'][1]['ref']]
        assert set(item['body'])=={'reason'}
        assert json.loads(item['body']['reason'])==value
        assert set(value)=={'verdict','goal_met','criterion_met','evidence_sufficient','support_quote','reason'}
        assert value['verdict'] in ('pass','fail','uncertain')
        assert all(type(value[k]) is bool for k in ('goal_met','criterion_met','evidence_sufficient'))
        assert isinstance(value['reason'],str) and 0<len(value['reason'])<=1000
        assert isinstance(value['support_quote'],str) and len(value['support_quote'])<=500
        if value['verdict']=='pass':
            assert all(value[k] is True for k in ('goal_met','criterion_met','evidence_sufficient'))
            assert value['support_quote'].strip() and value['support_quote'] in action['outcome']
    return len(audits)
