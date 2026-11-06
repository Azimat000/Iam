"""Два обычных сеанса уже принятого живого замысла; никаких подготовленных решений."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re

from iam.providers import CodexProvider, CodexSandboxConfig
from iam.providers.codex.auth import read_auth
from iam.providers.codex.command import REPO_ROOT, RESPONSE_JSON_SCHEMA
from iam.state import StateController, verify
from iam.storage import Store
from iam.stubs import ScriptedProvider, Reply, rest
from integration import intention_probe as probe
from integration.answer_audit import record_answer
from integration.test_persistent_core import tick, wire

MAX_CALLS, SESSION_CALLS, DECISIONS = 12, 6, 2


def run_continuation(source, output, *, mode='stub', auth_file=None, provider_factory=None,
                     image='iam-codex:1'):
    source, output = Path(source).resolve(), Path(output).resolve()
    if mode not in ('stub','live') or (mode=='live' and not auth_file):
        raise ValueError('Invalid mode or credentials')
    if mode=='live' and provider_factory is None and not re.fullmatch(r'sha256:[0-9a-f]{64}',image):
        raise ValueError('Immutable image required')
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('New external evidence directory required')
    auth=Path(auth_file).resolve() if auth_file else None
    if auth and auth.is_relative_to(output):raise ValueError('Credentials outside evidence required')
    if mode=='live' and provider_factory is None:
        inherited=json.loads((source.parents[1]/'manifest.json').read_text())
        finished=json.loads((source.parents[1]/'summary.json').read_text())
        if (inherited.get('protocol')!='options' or inherited.get('mode')!='live'
                or inherited.get('model')!=probe.series.MODEL or finished.get('status')!='completed'
                or not source.name.endswith('-options_visible')):
            raise ValueError('Completed real visible-options source required')
    previous=json.loads((source/'result.json').read_text())
    if len(previous['new_intentions'])!=1:raise ValueError('Exactly one actual accepted goal required')
    iid,goal=next(iter(previous['new_intentions'].items()))
    with Store.open(source/'persona.db') as store:
        ctl=StateController(store)
        if verify(store) or store.integrity_problems() or ctl.state['intentions'].get(iid)!=goal:
            raise ValueError('Source state does not match actual accepted goal')
        prefix=[event.to_dict() for event in store.events()]
    source_sha=probe.series.sha(source/'persona.db')
    output.mkdir(parents=True,mode=0o700)
    artifacts=probe.series.audit.Artifacts(output,private_paths=[str(auth)] if auth else [])
    probe.snapshot(source/'persona.db',output/'persona.db')
    frozen=probe.frozen_sources()
    manifest={'version':'s25-goal-continuation-1','mode':mode,'model':probe.series.MODEL,'image':image,
              'process_id':os.getpid(),
              'sources':frozen,'response_schema':RESPONSE_JSON_SCHEMA,'source_db_sha256':source_sha,
              'source_result_sha256':probe.series.sha(source/'result.json'),'prefix_events':len(prefix),
              'goal_id':iid,'original_goal':goal,'max_decisions':DECISIONS,'max_model_calls':MAX_CALLS,
              'session_calls':SESSION_CALLS,'native_retries':0,'cycle_retries':0,'injected_inputs':0,
              'behavioral_success':None}
    artifacts.write('manifest.json',manifest,exclusive=True)
    manifest_sha=probe.series.sha(output/'manifest.json')
    summary={'status':'running','decisions':0,'model_calls':0,'attempts':[],'sessions':[],
             'behavioral_success':None}
    def save():artifacts.write('summary.json',summary)
    def check_frozen():
        if (probe.frozen_sources()!=frozen or probe.series.sha(output/'manifest.json')!=manifest_sha
                or probe.series.sha(source/'persona.db')!=source_sha):
            raise probe.series.StopSeries('frozen_sources_changed')
    save()
    try:
        for number in range(1,DECISIONS+1):
            check_frozen()
            # Закрытие соединения между итерациями — фактический перезапуск постоянных модулей.
            with Store.open(output/'persona.db') as store:
                ctl=StateController(store)
                if [event.to_dict() for event in store.events()][:len(prefix)]!=prefix:
                    raise probe.series.StopSeries('source_prefix_changed')
                active={};session_calls=0
                def transport(argv,stdin,timeout,cleanup=None):
                    nonlocal session_calls
                    if summary['model_calls']>=MAX_CALLS or session_calls>=SESSION_CALLS:
                        raise probe.series.StopSeries('physical_call_cap')
                    active['session'].charge()
                    session_calls+=1;summary['model_calls']+=1
                    attempt={'index':summary['model_calls'],'session':number,'status':'reserved'}
                    summary['attempts'].append(attempt);save()
                    credentials=read_auth(str(auth),REPO_ROOT) if auth and provider_factory is None else None
                    try:
                        result=probe.subprocess_runner(argv,stdin,min(timeout,60),cleanup)
                        record_answer(artifacts,f'sessions/{number}',attempt['index'],result,auth=credentials)
                    except Exception as error:
                        attempt.update(status='error',error_class=type(error).__name__);save();raise
                    attempt.update(status='returned',returncode=result.returncode,timed_out=result.timed_out)
                    save();return result
                inner=(provider_factory(number,transport) if provider_factory else
                       CodexProvider(transport,CodexSandboxConfig(auth_file=str(auth),image=image,persist_auth_updates=True),
                                     model=probe.series.MODEL,timeout_s=60,max_format_retries=0) if mode=='live' else
                       ScriptedProvider([lambda request:Reply(()) if request['purpose']=='appraise' else Reply((rest(),))]*SESSION_CALLS))
                provider=probe.series.audit.AuditedProvider(probe.ProbeProvider(inner,active),artifacts,
                                                           f'sessions/{number}',disclose=False)
                session,cycle,_=wire(ctl,provider=provider,charge=mode=='stub',
                                     cycle_type=probe.OptionsExperimentCycle,max_rerequest=0)
                active.update(session=session,memory=cycle.memory)
                if ctl.session_state=='paused':session.command('stop','EXPERIMENTAL: закрытие прошлого сеанса')
                session.command('start','EXPERIMENTAL: обычное продолжение',max_calls=SESSION_CALLS)
                before=store.last_seq();recovered=ctl.state['intentions'][iid]
                summary['decisions']+=1;save()
                report=tick(cycle)
                entry={'number':number,'recovered_goal':recovered,'report':asdict(report),
                       'events':[event.to_dict() for event in ctl.events_after(before)],'traces':provider.traces,
                       'goal':ctl.state['intentions'][iid],'model_calls':session_calls,'usage':session.usage(),
                       'provider_failure':provider.failure}
                artifacts.write(f'sessions/{number}/result.json',entry)
                summary['sessions'].append({'number':number,'outcome':report.outcome,
                                            'goal_status':entry['goal']['status'],'model_calls':session_calls});save()
                if verify(store) or store.integrity_problems():raise probe.series.StopSeries('state_integrity')
                if provider.failure or report.outcome in ('failed','no_decision','skipped'):
                    raise probe.series.StopSeries('technical_cycle_failure')
                if ctl.session_state=='running':session.command('stop','EXPERIMENTAL: конец сеанса')
                artifacts.write(f'sessions/{number}/final.json',{'state':ctl.state,'memories':[r.to_dict() for r in cycle.memory.records()]})
        check_frozen();summary['status']='completed'
    except Exception as error:
        summary.update(status='stopped',error_class=type(error).__name__,diagnostic=artifacts.clean(str(error)))
    save();return summary


def main():
    parser=argparse.ArgumentParser()
    for key in ('source','output'):parser.add_argument('--'+key,required=True)
    parser.add_argument('--mode',choices=('stub','live'),default='stub')
    parser.add_argument('--auth-file');parser.add_argument('--image',default='iam-codex:1')
    args=parser.parse_args()
    result=run_continuation(args.source,args.output,mode=args.mode,auth_file=args.auth_file,image=args.image)
    print(json.dumps(result,ensure_ascii=False))
    return int(result['status']!='completed')


if __name__=='__main__':raise SystemExit(main())
