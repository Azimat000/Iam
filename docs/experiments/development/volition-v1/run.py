"""D-048: свободная мотивация без вопросов/напоминаний после остановок.

Четыре независимые копии двух прошлых, 6 сеансов × 3 решения. Цель не задаётся.
"""
import argparse
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from iam.backup import open_for_write
from iam.experiments import Persona
from iam.experiments.agency import CONTRACT, PERSONAL
from iam.experiments.development import DevelopmentRuntime
from iam.experiments.provenance import INSTRUCTION, REGISTRY, ProvenanceArchitecture
from iam.experiments.report.development import DAY, SessionBudget, fork_checkpoint, save_trace
from iam.experiments.results import Normalizer
from iam.experiments.volition import VERSION, INSTRUCTION as FIXED_INSTRUCTION, VolitionArchitecture
from iam.experiments.transfer import GUIDANCE
from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.run import subprocess_runner
from iam.state import StateController, verify
from iam.storage import Store

ROOT=Path(__file__).resolve().parents[4]
SWITCHES={'memory_off':False,'motivation_off':False,'interests_off':False,'relationships_off':False}
SESSIONS,CHOICES=6,3
ORDER=(('H1',1),('H2',2),('H2',1),('H1',2))
SOURCES={'H1':'free-fixed-1','H2':'free-fixed-2'}


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def domain(state):
    return {**{k:v for k,v in state.items() if k!='world_time'},'world':state['world_time']['world']}


def audit_source(directory,sha):
    raw=(directory/'trace.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('SHA256 исходного прошлого не совпал')
    trace=json.loads(raw)
    with Store.open(directory/'persona.db',readonly=True) as store:
        if store.store_kind!='experimental' or verify(store)+store.integrity_problems():raise ValueError('непроверенная экспериментальная база')
        ctl=StateController(store);events=ctl.events_after(0);normal=Normalizer()
        if normal([e.to_dict() for e in events])!=trace['journal'] or normal(ctl.state)!=trace['final_state']:
            raise ValueError('копия не соответствует исходной трассе')
        if ctl.session_state!='stopped' or ctl.state['intentions'] or REGISTRY not in ctl.state['world_time']['world']:
            raise ValueError('требуется остановленное прошлое с разметкой и без готовой цели')
        allowed={INSTRUCTION,PERSONAL+CONTRACT+GUIDANCE}
        if any(e.type in ('input.arrived','creator.reply') for e in events) or any(
                e.payload['request']['context'][0]['text'] not in allowed
                for e in events if e.type=='proposal.requested'):
            raise ValueError('исходная история содержит внешнее сообщение или дополнительный вопрос/подсказку')
        return {'state_sha256':digest(ctl.state),'journal_sha256':digest([e.to_dict() for e in events]),
                'prefix_seq':store.last_seq(),'life_ms':ctl.life_time_ms,'sha256':sha}


def run(source_root,output,auth=None,provider_factory=None):
    source_root,output=Path(source_root),Path(output)
    manifest=json.loads((source_root/'summary.json').read_text())
    if manifest['protocol']!='provenance-v1':raise ValueError('неверный протокол исходной серии')
    sources={}
    for label,code in SOURCES.items():
        row=next(r for r in manifest['runs'] if r['code']==code)
        if row['task']!='free' or row['profile']!='fixed' or row['status']!='executed':raise ValueError('нужна свободная исправленная история')
        path=source_root/Path(row['trace']).parent
        sources[label]={'directory':str(path.resolve()),**audit_source(path,row['sha256'])}
    output.mkdir(parents=True,exist_ok=False)
    live=provider_factory is None
    config=CodexSandboxConfig(auth_file=auth,persist_auth_updates=True) if live else None
    persona=Persona.load(ROOT/'scenarios/personas/curious.toml')
    pause=[False];runs=[];stop_reason=None
    summary={'protocol':'volition-v1','profile_version':VERSION,'live':live,'model':'gpt-6-luna' if live else 'scripted',
             'sessions_per_run':SESSIONS,'choices_per_session':CHOICES,'planned_decisions':len(ORDER)*SESSIONS*CHOICES,
             'sources':sources,'runs':runs,'human_ratings':None,
             'conditions':{'goal_questions':0,'reminders':0,'incoming_messages':0,'seeded_intentions':0,
                           'instruction_sha256':{k:hashlib.sha256(v.encode()).hexdigest() for k,v in {'control':INSTRUCTION,'fixed':FIXED_INSTRUCTION}.items()},
                           'technical_intention_contract_present':True},
             'limitation':'Моделируемые остановки и шесть коротких сеансов, не длительный реальный пилот и не доказательство личности.'}
    def persist():
        summary['provider_paused']=pause[0]
        summary['technical_pause_reason']=stop_reason
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    persist()
    for history,repeat in ORDER:
        row={'code':f'{history}-{repeat}','history':history,'repeat':repeat,'status':'not_run','calls':0,
             'sessions':0,'decisions':0,'reason':stop_reason if pause[0] else ''}
        runs.append(row)
        if pause[0]:persist();continue
        row['profile']='control' if repeat==1 else 'fixed'
        instruction=INSTRUCTION if repeat==1 else FIXED_INSTRUCTION
        architecture_type=ProvenanceArchitecture if repeat==1 else VolitionArchitecture
        directory=output/row['code'];fork_checkpoint(Path(sources[history]['directory']),directory)
        providers=[]
        def factory(runtime):
            raw=CodexProvider(subprocess_runner,config,max_format_retries=0) if live else provider_factory(runtime)
            p=SessionBudget(raw,pause);providers.append(p);return p
        decisions,snapshots=[],[]
        for number in range(SESSIONS):
            with open_for_write(directory/'persona.db') as store:
                runtime=DevelopmentRuntime(store,persona,SWITCHES,calendar_ms=(100+number)*DAY)
                before=copy.deepcopy(runtime.controller.state)
                if number==0:
                    assert digest(before)==sources[history]['state_sha256']
                    assert digest([e.to_dict() for e in runtime.controller.events_after(0)])==sources[history]['journal_sha256']
                    row['initial']=sources[history]
                else:
                    assert before==snapshots[-1]['state'], 'не восстановлено сохранённое состояние'
                    assert runtime.controller.life_time_ms==snapshots[-1]['life_ms'], 'время жизни увеличилось в остановке'
                runtime.step({'kind':'session','action':'start'})
                architecture=architecture_type(factory)
                cycle=architecture.build(runtime)
                restored=domain(before)==domain(runtime.controller.state)
                assert restored, 'перезапуск изменил личный опыт'
                for choice in range(CHOICES):
                    runtime.step({'kind':'wait','life_ms':60000});start=store.last_seq()
                    report=cycle.run(runtime.trigger(f'autonomy_{100+number}_{choice}'))
                    events=runtime.controller.events_after(start)
                    requests=[e.payload['request'] for e in events if e.type=='proposal.requested']
                    assert all(q['context'][0]['text']==instruction for q in requests), 'добавлена подсказка'
                    decisions.append({'session':number,'step':choice,'report':asdict(report),
                        'requests':requests,'proposals':[e.payload for e in events if e.type=='proposal.received'],
                        'applied':[e.payload for e in events if e.changes_state]})
                    if report.outcome in ('no_decision','failed') or report.reason=='bad_format':
                        pause[0]=True;stop_reason=report.reason or report.outcome;row['reason']=stop_reason
                    if pause[0]:break
                runtime.step({'kind':'session','action':'stop'})
                snapshots.append({'session':number,'restored':restored,'state':runtime.controller.state,
                                  'seq':store.last_seq(),'life_ms':runtime.controller.life_time_ms})
            result,errors=save_trace(directory,architecture,persona,decisions,snapshots,SWITCHES)
            if errors:pause[0]=True;stop_reason='integrity_failure';row['reason']=stop_reason
            if pause[0]:
                result.trace['status']='technical_pause';(directory/'trace.json').write_bytes(result.trace_bytes)
            row.update(status='technical_pause' if pause[0] else 'running',sessions=len(snapshots),decisions=len(decisions),
                       calls=sum(p.calls for p in providers),integrity_problems=errors,trace=row['code']+'/trace.json',
                       sha256=hashlib.sha256(result.trace_bytes).hexdigest())
            persist()
            print(row['code'],'session',number+1,'decisions',row['decisions'],'calls',row['calls'],
                  'intentions',len(snapshots[-1]['state']['intentions']),'errors',len(errors),flush=True)
            if pause[0]:break
        if not pause[0]:row['status']='completed'
        persist()
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-root',required=True);p.add_argument('--output',required=True);p.add_argument('--auth-file',required=True)
    a=p.parse_args();run(a.source_root,a.output,a.auth_file)
