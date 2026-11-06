"""Одна непрерывная A/E-история; каждый сеанс в отдельном процессе, без заданных целей."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from iam.providers import CodexProvider, CodexSandboxConfig
from iam.providers.codex.auth import read_auth
from iam.providers.codex.command import REPO_ROOT, RESPONSE_JSON_SCHEMA
from iam.state import StateController, verify
from iam.cycle.validator import tool_use_signs
from iam.intentions.completion import valid_response
from iam.storage import Store
from iam.stubs import ScriptedProvider, Reply, rest
from integration import intention_probe as probe
from integration.answer_audit import record_answer
from integration.test_persistent_core import tick, wire

VERSION = 's25-post-partial-1'
STORIES, SESSIONS, DECISIONS = ('A', 'E'), 3, 2
MAX_CALLS, SESSION_CALLS, FORECAST = 60, 10, 6
DESIGN = 'docs/evidence/S25-post-partial-design.md'


def sources():
    repair='docs/evidence/S25-completion-precheck-design.md'
    return {**probe.frozen_sources(), DESIGN: probe.series.sha(probe.series.ROOT / DESIGN),
            repair:probe.series.sha(probe.series.ROOT/repair)}


def load(root, name):
    return json.loads((root / name).read_text())


def artifacts(root, auth):
    return probe.series.audit.Artifacts(root, private_paths=[str(auth)] if auth else [])


def check(root, manifest, summary):
    if (sources() != manifest['sources'] or probe.series.sha(root/'manifest.json') != summary['manifest_sha256']
            or any(probe.series.sha(root/item['db']) != item['sha256']
                   or probe.series.sha(Path(item['source'])/'persona.db') != item['source_sha256']
                   or probe.series.sha(Path(item['source'])/'result.json') != item['result_sha256']
                   for item in manifest['initial'].values())):
        raise probe.series.StopSeries('frozen_sources_changed')


def run_session(root, name, auth_file=None, provider_factory=None):
    """Только родитель запускает следующий заранее заданный сеанс, без повторов."""
    root=Path(root); manifest=load(root,'manifest.json'); summary=load(root,'summary.json')
    auth=Path(auth_file).resolve() if auth_file else None
    output=artifacts(root,auth)
    def save():output.write('summary.json',summary)
    try:
        check(root,manifest,summary)
        schedule=manifest['schedule']
        if (summary['status']!='running' or len(summary['sessions'])>=len(schedule)
                or name!=schedule[len(summary['sessions'])]
                or any(row['status']!='completed' for row in summary['sessions'])):
            raise probe.series.StopSeries('session_order_or_replay')
        story=name.split('-')[0]; initial=manifest['initial'][story]
        with Store.open(root/initial['db'],readonly=True) as store:
            prefix=[event.to_dict() for event in store.events()]
        record={'name':name,'process_id':os.getpid(),'status':'running','decisions':[]}
        summary['sessions'].append(record); save()
        with Store.open(root/f'stories/{story}/persona.db') as store:
            if [event.to_dict() for event in store.events()][:len(prefix)]!=prefix:
                raise probe.series.StopSeries('prefix_changed')
            ctl=StateController(store); active={}; session_calls=0; decision_calls=0
            def transport(argv,stdin,timeout,cleanup=None):
                nonlocal session_calls,decision_calls
                check(root,manifest,summary)
                if (summary['model_calls']>=manifest['max_model_calls'] or session_calls>=SESSION_CALLS
                        or decision_calls>=FORECAST):
                    raise probe.series.StopSeries('physical_call_cap')
                active['session'].charge()
                session_calls+=1; decision_calls+=1; summary['model_calls']+=1
                attempt={'index':summary['model_calls'],'session':name,'decision':summary['decisions'],
                         'status':'reserved','transport_invoked':False}
                summary['attempts'].append(attempt); save()
                credentials=read_auth(str(auth),REPO_ROOT) if auth and provider_factory is None else None
                try:
                    left=(active['session'].config()['max_ms']-active['session'].usage()['elapsed_ms'])/1000
                    if left<=0:raise probe.series.StopSeries('session_deadline')
                    attempt['transport_invoked']=True;save()
                    result=probe.subprocess_runner(argv,stdin,min(timeout,60,left),cleanup)
                    record_answer(output,'sessions/'+name,attempt['index'],result,auth=credentials)
                except Exception as error:
                    attempt.update(status='error',error_class=type(error).__name__);save();raise
                attempt.update(status='returned',returncode=result.returncode,timed_out=result.timed_out);save()
                return result
            inner=(provider_factory(name,transport) if provider_factory else
                   CodexProvider(transport,CodexSandboxConfig(auth_file=str(auth),image=manifest['image'],
                                   persist_auth_updates=True),model=probe.series.MODEL,timeout_s=60,max_format_retries=0)
                   if manifest['mode']=='live' else ScriptedProvider([
                       lambda request:Reply(()) if request['purpose']=='appraise' else Reply((rest(),))]*20))
            provider=probe.series.audit.AuditedProvider(probe.ProbeProvider(inner,active),output,'sessions/'+name,disclose=False)
            session,cycle,_=wire(ctl,provider=provider,charge=manifest['mode']=='stub',
                                 cycle_type=probe.OptionsExperimentCycle,max_rerequest=0)
            active.update(session=session,memory=cycle.memory)
            if ctl.session_state=='paused':session.command('stop','EXPERIMENTAL: закрыть прошлый сеанс')
            if ctl.session_state!='stopped':raise probe.series.StopSeries('source_session_running')
            record.update(recovered_goals=ctl.state['intentions'],before_seq=store.last_seq())
            session.command('start','EXPERIMENTAL: обычный сеанс',max_calls=SESSION_CALLS,max_ms=1_800_000)
            record['start_seq']=store.last_seq();save()
            for index in range(DECISIONS):
                check(root,manifest,summary)
                if session.usage()['max_calls']-session.usage()['calls']<FORECAST:
                    record['planned_stop']='session_forecast';break
                if summary['decisions']>=manifest['max_decisions']:raise probe.series.StopSeries('decision_cap')
                before=store.last_seq(); trace_before=len(provider.traces); decision_calls=0
                summary['decisions']+=1;save()
                report=tick(cycle)
                events=[event.to_dict() for event in ctl.events_after(before)]
                errors=probe.series.audit.assessment_errors(events)
                traces=provider.traces[trace_before:]
                faults,_,_,_=probe.series.audit.decision_technical_faults(asdict(report),events,traces)
                # Как в полной серии S25: недоступный мост сохраняет просьбу, не выдумывает доставку.
                faults=[fault for fault in faults if not (fault['source']=='event'
                        and fault['event']['payload'].get('fault')=='creator_undelivered')]
                faults.extend({'source':'event','event':event} for event in events if event['type']=='proposal.stale')
                for trace in traces:
                    response=trace.get('response');query=trace['request']
                    if (isinstance(response,dict) and (response.get('tool_use_detected') or tool_use_signs(response)
                            or any(response.get(key)!=query[key] for key in ('request_id','epoch','state_version')))):
                        faults.append({'source':'trace','request_id':query['request_id'],'reason':'tool_or_stale_echo'})
                entry={'index':index+1,'report':asdict(report),'events':events,'traces':traces,
                       'state':ctl.state,'usage':session.usage(),'model_calls':decision_calls,
                       'assessment_errors':errors,'technical_faults':faults,'provider_failure':provider.failure}
                output.write(f'sessions/{name}/decisions/{index+1:02d}.json',entry)
                record['decisions'].append({'index':index+1,'outcome':report.outcome,'reason':report.reason,
                                            'model_calls':decision_calls});save()
                if verify(store) or store.integrity_problems():raise probe.series.StopSeries('state_integrity')
                if (provider.failure or errors or faults or report.outcome in ('failed','no_decision','skipped')
                        or not any(row['request']['purpose']=='choose_action' and row.get('error') is None
                                   and valid_response(row.get('response'),row['request']) for row in traces)):
                    raise probe.series.StopSeries('technical_cycle_failure')
                if ctl.session_state!='running':raise probe.series.StopSeries('unexpected_pause')
            session.command('stop','EXPERIMENTAL: конец сеанса')
            record.update(status='completed',goals=ctl.state['intentions'],usage=session.usage(),after_seq=store.last_seq())
            output.write('sessions/'+name+'/final.json',{'record':record,'state':ctl.state,
                          'memories':[row.to_dict() for row in cycle.memory.records()]})
        check(root,manifest,summary);save();return 0
    except Exception as error:
        if summary['status']=='running':
            summary.update(status='stopped',error_class=type(error).__name__,diagnostic=output.clean(str(error)))
            save()
        return 1


def run_series(source, output, *, mode='stub', auth_file=None, image='iam-codex:1', provider_factory=None):
    source,output=Path(source).resolve(),Path(output).resolve()
    auth=Path(auth_file).resolve() if auth_file else None
    if mode not in ('stub','live') or (mode=='live' and not auth):raise ValueError('Invalid mode or credentials')
    if mode=='live' and provider_factory is None and not re.fullmatch(r'sha256:[0-9a-f]{64}',image):
        raise ValueError('Immutable image required')
    if output.exists() or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError('New external evidence directory required')
    if auth and auth.is_relative_to(output):raise ValueError('Credentials outside evidence required')
    inherited=load(source,'manifest.json'); finished=load(source,'summary.json')
    if (inherited.get('protocol')!='options' or inherited.get('model')!=probe.series.MODEL
            or finished.get('status')!='completed' or (mode=='live' and provider_factory is None
                                                        and inherited.get('mode')!='live')):
        raise ValueError('Completed real options source required')
    info={}
    for story in STORIES:
        cell=source/f'cells/{story}-options_visible'; result=load(cell,'result.json')
        with Store.open(cell/'persona.db',readonly=True) as store:
            state=store.latest_snapshot().state
            if (verify(store) or store.integrity_problems() or any(state['intentions'].get(key)!=value
                  for key,value in result['new_intentions'].items())):raise ValueError('Inherited goal mismatch')
        info[story]={'source':str(cell),'source_sha256':probe.series.sha(cell/'persona.db'),
                     'result_sha256':probe.series.sha(cell/'result.json'),'goals':state['intentions']}
    output.mkdir(parents=True,mode=0o700); artifacts_out=artifacts(output,auth)
    for story,item in info.items():
        target=output/f'initial/{story}.db';probe.snapshot(Path(item['source'])/'persona.db',target)
        item.update(db=f'initial/{story}.db',sha256=probe.series.sha(target))
        probe.snapshot(target,output/f'stories/{story}/persona.db')
    manifest={'version':VERSION,'mode':mode,'model':probe.series.MODEL,'image':image,'sources':sources(),
              'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=probe.series.ROOT,text=True).strip(),
              'initial':info,'source_manifest_sha256':probe.series.sha(source/'manifest.json'),
              'response_schema':RESPONSE_JSON_SCHEMA,'profile_sha256':probe.series.sha(probe.series.PROFILE_PATH),
              'schedule':[f'{story}-{number}'for story in STORIES for number in range(1,SESSIONS+1)],
              'max_decisions':len(STORIES)*SESSIONS*DECISIONS,'max_model_calls':MAX_CALLS,
              'session_calls':SESSION_CALLS,'session_ms':1_800_000,'forecast':FORECAST,'native_retries':0,
              'cycle_retries':0,'injected_inputs':0,'behavioral_success':None,
              'worker_mode':'inline_test_only' if provider_factory is not None else 'subprocess'}
    artifacts_out.write('manifest.json',manifest,exclusive=True)
    summary={'status':'running','manifest_sha256':probe.series.sha(output/'manifest.json'),
             'decisions':0,'model_calls':0,'attempts':[],'sessions':[],'behavioral_success':None}
    artifacts_out.write('summary.json',summary)
    try:
        for name in manifest['schedule']:
            if provider_factory is not None:code=run_session(output,name,auth,provider_factory)
            else:
                argv=[sys.executable,'-m','integration.goal_series','--worker-root',str(output),'--session',name]
                if auth:argv.extend(['--auth-file',str(auth)])
                env={**os.environ,'PYTHONPATH':os.pathsep.join(str(probe.series.ROOT/path) for path in ('src','tests'))}
                code=subprocess.run(argv,cwd=probe.series.ROOT,env=env,
                                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode
            summary=load(output,'summary.json')
            if code or summary['status']!='running':raise probe.series.StopSeries('worker_failure')
        check(output,manifest,summary);summary['status']='completed'
    except Exception as error:
        summary=load(output,'summary.json')
        if summary['status']=='running':summary.update(status='stopped',error_class=type(error).__name__,diagnostic=artifacts_out.clean(str(error)))
    artifacts_out.write('summary.json',summary);return summary


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source');parser.add_argument('--output');parser.add_argument('--mode',choices=('stub','live'),default='stub')
    parser.add_argument('--auth-file');parser.add_argument('--image',default='iam-codex:1')
    parser.add_argument('--worker-root');parser.add_argument('--session')
    args=parser.parse_args()
    if args.worker_root:return run_session(args.worker_root,args.session,args.auth_file)
    if not args.source or not args.output:parser.error('source and output required')
    summary=run_series(args.source,args.output,mode=args.mode,auth_file=args.auth_file,image=args.image)
    print(json.dumps(summary,ensure_ascii=False));return int(summary['status']!='completed')


if __name__=='__main__':raise SystemExit(main())
