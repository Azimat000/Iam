"""Наблюдаемые цепочки целей/возвратов. Смысл замысла проверяется человеком отдельно."""
from collections import Counter,defaultdict
import json

PENDING={'active','deferred','waiting_opportunity'}


def assess(trace,prefix,initial_projects):
    events=trace['journal'];by_event={e['event_id']:e for e in events}
    proposals={p['proposal_id']:p for e in events if e['type']=='proposal.received' for p in e['payload']['response']['proposals']}
    requests={e['payload']['request']['request_id']:e for e in events if e['type']=='proposal.requested'}
    accepted={e['payload']['proposal_id']:e for e in events if e['type']=='proposal.accepted'}
    final=trace['final_state'];actions=final['world_time']['world']['actions']
    creates=[e for e in events if e['seq']>prefix and e['type']=='proposal.accepted' and e['payload']['kind']=='intention'
             and proposals[e['payload']['proposal_id']]['body']['op']=='create']
    result=[]
    for creation in creates:
        p=proposals[creation['payload']['proposal_id']];body=p['body'];key=body['id'];origin=requests[creation['payload']['request_id']]
        later=[];opportunities=[];transitions=[];all_later=[]
        for e in events:
            if e['seq']>creation['seq'] and e['type']=='proposal.accepted' and e['payload']['kind']=='intention':
                pr=proposals[e['payload']['proposal_id']]
                if pr['body']['id']==key and pr['body']['op']=='transition':
                    transitions.append({'seq':e['seq'],'status':pr['body']['to_status'],'result_ref':pr['body'].get('result_ref'),'result_quote':pr['body'].get('result_quote')})
        for d in trace['decision_trace']:
            q=d['requests'][0];request=requests[q['request_id']]
            if request['seq']<=creation['seq'] or q['session_id']==origin['payload']['request']['session_id']:continue
            visible=json.loads(q['context'][-1]['text'])['development']['intentions'].get(key)
            if not visible or visible['status'] not in PENDING:continue
            opportunities.append({'session':d['session'],'step':d['step'],'status':visible['status']})
            for a in d['report']['actions']:
                stored=actions[a['action_id']];params=stored['params']
                if a['status']=='confirmed':
                    all_later.append({'session':d['session'],'step':d['step'],'operation':params.get('operation',stored['kind']),
                        'target':params.get('target'),'rationale':proposals[by_event[a['events'][0]]['payload']['proposal_id']]['rationale'],
                        'result_text':params.get('text',stored.get('outcome'))})
                if (a['status']!='confirmed' or stored['kind']!='action' or params.get('target')!=body['target']
                        or params.get('operation')!=body['operation']):continue
                event=by_event[a['events'][0]];pr=proposals[event['payload']['proposal_id']]
                later.append({'session':d['session'],'step':d['step'],'operation':params['operation'],
                    'event':event['event_id'],'proposal':pr['proposal_id'],'goal_creation_cited':creation['event_id'] in pr['cites'],
                    'rationale':pr['rationale'],'result_text':params.get('text',stored.get('outcome')),'goal_was_visible_as_own_state':True})
        result.append({'id':key,'text':body['text'],'target':body['target'],'operation':body['operation'],'criterion':body['criterion'],'evidence_kind':body['evidence_kind'],'created_event':creation['event_id'],
                       'created_seq':creation['seq'],'creation_rationale':p['rationale'],'sources':final['intentions'][key]['sources'],
                       'final_status':final['intentions'][key]['status'],'later_session_opportunities':opportunities,
                       'matching_later_actions':later,'later_action_candidates':all_later,'transitions':transitions,
                       'future_meaning':'requires_review','semantic_fulfilment':'requires_review','specificity':'requires_review',
                       'return_is_causal_proof':False})
    counts=Counter();project_sessions=defaultdict(set);operations=[]
    proposed=[];rejected=[]
    for d in trace['decision_trace']:
        counts['decisions']+=1;counts['attempts']+=len(d['requests']);counts['cycle_'+d['report']['outcome']]+=1
        for pack in d['proposals']:
            for p in pack['response']['proposals']:
                if p['kind']=='intention' and p['body'].get('op')=='create':
                    proposed.append({'id':p['body'].get('id'),'text':p['body'].get('text'),'operation':p['body'].get('operation'),'target':p['body'].get('target'),'criterion':p['body'].get('criterion'),'session':d['session'],'step':d['step']})
                    if p['proposal_id'] not in accepted:rejected.append(p['proposal_id'])
                if p['proposal_id'] in accepted and p['kind']=='memory_write':counts['accepted_interpretations']+=1
        for a in d['report']['actions']:
            stored=actions[a['action_id']];params=stored['params'];op=params.get('operation',stored['kind'])
            counts['action_'+a['status']]+=1;counts[op]+=1
            operations.append({'session':d['session'],'step':d['step'],'operation':op,'target':params.get('target'),
                               'status':a['status']})
            if a['status']=='confirmed' and op=='write_project':project_sessions[params['target']].add(d['session'])
    return {'counts':dict(counts),'proposed_goal_creations':proposed,'rejected_goal_proposals':rejected,
            'intention_candidates':result,'accepted_intention_count':len(result),
            'projects_with_new_writes':[{'project':key,'sessions':sorted(sessions),'new_project':key not in initial_projects}
                                        for key,sessions in sorted(project_sessions.items())],
            'operations':operations,'human_ratings':None,
            'limitation':'Совпадение operation/target — кандидат на возврат, смысл сверяется по неизменному criterion. Действия при deferred/waiting не считаются автоматически целевым возвращением. Без create это продолжение занятия.'}
