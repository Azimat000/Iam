"""Независимая сверка двух стадий; не вызывает Runtime/parse_* оценщика."""
import hashlib
import json
from iam.domain import jsonschema
from iam.experiments.fidelity import REQUIREMENT, REVIEW


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def stage_check(stage,parent,instruction,packet,actor,live,fields):
    query=stage['request'];assert not jsonschema.errors('request',query)
    assert len(query['context'])==2 and query['context'][0]['text']==instruction
    assert query['context'][0]['origin']=='model_knowledge' and query['context'][0]['layer']=='state'
    assert query['context'][1]['origin']=='observation' and query['context'][1]['layer']=='state'
    assert json.loads(query['context'][1]['text'])==packet
    assert query['request_id']!=parent['request_id'] and query['idempotency_key']=='req:'+query['request_id']
    assert 'retry_of' not in query and query['limits']['attempt']==0
    assert 0<query['limits']['deadline_ms']<=parent['limits']['deadline_ms']
    assert query['limits']['max_output_tokens']==parent['limits']['max_output_tokens']
    assert query['allowed_proposals']==['no_action'] and query['allowed_actions']==[] and query['purpose']=='appraise'
    assert query['epoch']==parent['epoch'] and query['state_version']==parent['state_version']
    answer=stage['response'];value=stage['assessment']
    if not stage['attempted']:assert answer is None and value is None and stage['error']=='assessment_deadline'
    if value is None:
        assert stage['error'];return None
    assert stage['error'] is None and not jsonschema.errors('response',answer)
    assert answer['status']=='ok' and answer['tool_use_detected'] is False
    assert all(answer[k]==query[k] for k in ('request_id','epoch','state_version'))
    assert answer['provider']==actor['provider']
    if live:assert answer['provider']=={'name':'codex','model':'gpt-6-luna'}
    assert len(answer['proposals'])==1
    item=answer['proposals'][0];assert item['kind']=='no_action' and item['cites']==[query['context'][1]['ref']]
    assert item['body']==value and set(value)==fields
    assert isinstance(value['reason'],str) and 0<len(value['reason'])<=1000
    assert isinstance(value['support_quote'],str) and len(value['support_quote'])<=500
    return value


def check(audits,events,store,scopes,live):
    proposals={p['proposal_id']:(p,e.payload['response']) for e in events if e.type=='proposal.received' for p in e.payload['response']['proposals']}
    requests={e.payload['request']['request_id']:e for e in events if e.type=='proposal.requested'}
    by_id={e.event_id:e for e in events}
    for audit in audits:
        payload=audit.payload;pr,actor=proposals[payload['proposal_id']]
        event=requests[actor['request_id']];q=event.payload['request'];state=store.get_snapshot(q['epoch'],q['state_version']).state
        assert pr['kind']=='intention' and pr['body']['to_status']=='done'
        ref=pr['body']['result_ref'];result=by_id[ref];action=state['world_time']['world']['actions'][result.payload['action_id']]
        shown=next(c['text'] for c in q['context'] if c['ref']==ref)
        meta=json.loads(q['context'][-2]['text'])['experience_sources'][ref]
        assert result.type=='action.confirmed' and ref in pr['cites']
        goal=state['intentions'][pr['body']['id']]
        packet={'goal':goal,'visible_context':q['context'][1:],
            'available_result':{'text':shown,'base':ref,'social':meta['social'],'operation':meta['operation'],
                'self_quote':meta['self_quote'],'scopes':scopes(action,state,ref),'outcome':action['outcome'],'action_id':result.payload['action_id']}}
        assert action['outcome'] in shown
        assert payload['binding']==digest({'request':q,'proposal':pr,'packet':packet,'epoch':q['epoch'],'state_version':q['state_version'],
            'requirement_instruction':REQUIREMENT,'review_instruction':REVIEW})
        blind={'text':goal['text'],'criterion':goal.get('criterion'),'operation':goal.get('operation','write_project'),'target':goal['project']}
        needed=stage_check(payload['requirement'],q,REQUIREMENT,{'goal':blind},actor,live,{'required_kind','support_quote','reason'})
        match=False
        if needed:
            assert needed['required_kind'] in ('artifact','received_text','independent_observation','uncertain')
            if needed['required_kind']!='uncertain':assert needed['support_quote'].strip() and any(needed['support_quote'] in (goal.get(k) or '') for k in ('text','criterion'))
            match=needed['required_kind'] in packet['available_result']['scopes']
        assert payload['scope_match'] is match
        evaluation=payload['evaluation'];value=None
        if not match:assert evaluation is None
        else:
            assert evaluation is not None
            value=stage_check(evaluation,q,REVIEW,{**packet,'required_kind':needed['required_kind']},actor,live,
                {'verdict','goal_met','criterion_met','evidence_sufficient','support_quote','reason'})
            if value:
                assert value['verdict'] in ('pass','fail','uncertain')
                assert all(type(value[k]) is bool for k in ('goal_met','criterion_met','evidence_sufficient'))
                if value['verdict']=='pass':
                    assert all(value[k] is True for k in ('goal_met','criterion_met','evidence_sufficient'))
                    assert value['support_quote'].strip() and value['support_quote'] in action['outcome']
        assert payload['assessment']==value
        assert payload['error']==(payload['requirement']['error'] or (evaluation['error'] if evaluation else None))
        assert payload['passed'] is bool(not payload['error'] and match and value and value['verdict']=='pass')
        assert not audit.changes_state and payload['phase']=='completion_assessment'
    return len(audits)
