"""Заданные сохранённые случаи D049: проверка оценщика, не мотивации личности."""
import argparse
import hashlib
import json
from pathlib import Path

from iam.experiments.provenance import REGISTRY
from iam.experiments.fidelity import REVIEW, parse_review, review_request
from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.run import subprocess_runner
from iam.state import StateController
from iam.storage import Store

CASES=(('N-1','test_candidate_rule','false_done_craft','deny'),
       ('N-2','check_sequence_n6','false_done_self_prediction','deny'),
       ('N-1','find_discriminating_observation','design_only','pass'),
       ('N-1','compare_rules_at_seven','own_artifact_check','pass'))


def prepare(root,output):
    root,output=Path(root),Path(output);cases=[]
    for code,key,label,expected in CASES:
        with Store.open(root/code/'persona.db',readonly=True) as store:
            ctl=StateController(store);events=ctl.events_after(0);final=ctl.state['intentions'][key]
            # Для исторического done — точный исходный запрос и результат, без позднего оправдания.
            if final['status']=='done':
                pr=next(p for e in events if e.type=='proposal.received' for p in e.payload['response']['proposals']
                        if p['kind']=='intention' and p['body'].get('id')==key and p['body'].get('to_status')=='done')
                response=next(e.payload['response'] for e in events if e.type=='proposal.received' and pr in e.payload['response']['proposals'])
                q=next(e.payload['request'] for e in events if e.type=='proposal.requested' and e.payload['request']['request_id']==response['request_id'])
                ref=pr['body']['result_ref']
            else:
                # Узкий незавершённый замысел и реально позднее открытие собственного результата.
                eligible=[]
                for event in events:
                    if event.type!='proposal.requested':continue
                    q0=event.payload['request'];state=store.get_snapshot(q0['epoch'],q0['state_version']).state
                    for ref0 in {c['ref'] for c in q0['context']}:
                        basis=ctl.find_event(event_id=ref0)
                        if not basis or basis.type!='action.confirmed' or basis.seq<=final['created_seq']:continue
                        a=state['world_time']['world']['actions'].get(basis.payload['action_id'])
                        if a and a['params']['operation']==final['operation'] and a['params']['target']==final['project']:
                            eligible.append((q0,ref0))
                assert eligible,(key,'нет доступного позднего результата')
                q,ref=max(eligible,key=lambda item:ctl.find_event(event_id=item[1]).seq)
            state=store.get_snapshot(q['epoch'],q['state_version']).state;goal=state['intentions'][key]
            basis=ctl.find_event(event_id=ref);action=state['world_time']['world']['actions'][basis.payload['action_id']]
            shown=next(c['text'] for c in q['context'] if c['ref']==ref)
            assert action['outcome'] in shown
            meta=json.loads(q['context'][-2]['text'])['experience_sources'][ref]
            operation=action['params']['operation']
            if operation=='write_project':kind=['artifact']
            elif operation=='read_material':kind=['received_text']
            else:
                assert operation=='open_project'
                origin=state['world_time']['world'][REGISTRY]['outcomes'][ref]['content_origin']
                kind=['received_text','artifact'] if origin in ('model_written_version','scripted_version') else ['received_text']
            packet={'goal':goal,'available_result':{'text':shown,'base':ref,'operation':meta['operation'],'social':meta['social'],
                    'self_quote':meta['self_quote'],'outcome':action['outcome'],'action_id':basis.payload['action_id'],'scopes':kind},
                    'visible_context':q['context'][1:]}
            cases.append({'id':label,'source':code,'goal_id':key,'expected':expected,'parent':q,'packet':packet})
    data={'protocol':'fidelity-corpus-v1','spontaneous_motivation':False,'review_instruction_sha256':hashlib.sha256(REVIEW.encode()).hexdigest(),'cases':cases}
    output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');return data


def run(corpus,output,auth):
    raw=Path(corpus).read_bytes();data=json.loads(raw);results=[]
    assert data['review_instruction_sha256']==hashlib.sha256(REVIEW.encode()).hexdigest()
    provider=CodexProvider(subprocess_runner,CodexSandboxConfig(auth_file=auth,persist_auth_updates=True),max_format_retries=0)
    result={'protocol':'fidelity-corpus-v1','spontaneous_motivation':False,'corpus_sha256':hashlib.sha256(raw).hexdigest(),'results':results}
    for case in data['cases']:
        query=review_request(case['parent'],case['packet']);answer=provider.propose(query)
        assessment=parse_review(answer,query,{'name':'codex','model':'gpt-6-luna'},case['packet']['available_result']['outcome'])
        matched=assessment is not None and (assessment['verdict']=='pass' if case['expected']=='pass' else assessment['verdict'] in ('fail','uncertain'))
        results.append({'id':case['id'],'expected':case['expected'],'request':query,'response':answer,'assessment':assessment,'matched':matched})
        Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(case['id'],assessment['verdict'] if assessment else 'invalid','matched',matched,flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['prepare','run']);p.add_argument('--input',required=True);p.add_argument('--output',required=True);p.add_argument('--auth-file')
    a=p.parse_args()
    if a.mode=='prepare':prepare(a.input,Path(a.output))
    else:run(a.input,a.output,a.auth_file)
