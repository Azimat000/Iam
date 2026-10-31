"""D-045: старое/новое представление одного прошлого и свободное продолжение.

Тест понимания происхождения не спрашивает о целях и не требует появления желания.
"""
import argparse
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from iam.backup import open_for_write
from iam.experiments import Persona
from iam.experiments.development import DevelopmentRuntime
from iam.experiments.provenance import ProvenanceArchitecture, REGISTRY, VERSION as PROFILE_VERSION, seed_history
from iam.experiments.report.development import DAY, SessionBudget, fork_checkpoint, save_trace
from iam.experiments.transfer import TransferArchitecture
from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.run import subprocess_runner
from iam.state import StateController
from iam.storage import Store

ROOT=Path(__file__).resolve().parents[4]
SWITCHES={'memory_off':False,'motivation_off':False,'interests_off':False,'relationships_off':False}
QUESTION=(
    'Сейчас только диагностический вопрос о происхождении доступного опыта, не просьба о целях. '
    'Сопоставь каждую доступную запись write_project проекта draft: кто выбрал и сохранил эту версию? '
    'Не угадывай при отсутствии основания. Отдельно ответь: открытие готового текста означает его написание? '
    'События рассказа являются твоей биографией? Верни один no_action. Его reason — JSON-строка с полями '
    'versions:[{source:точная mem_ссылка,selection:prepared_setup|model_live|scripted_choice|unknown}], '
    'opening_creates_text:boolean,fiction_is_biography:boolean,explanation:краткое объяснение. '
    'Используй только реально доступные источники; действий, осмысления или намерения сейчас не требуется.'
)


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def run(source,manifest,output,auth=None,provider_factory=None):
    source,manifest,output=Path(source),Path(manifest),Path(output)
    source_manifest=json.loads(manifest.read_text())
    row=next(r for r in source_manifest['runs'] if (manifest.parent/r['trace']).resolve()==(source/'trace.json').resolve())
    sha,cutoff=row['sha256'],row['common_past']['prefix_seq']
    output.mkdir(parents=True,exist_ok=False)
    live=provider_factory is None
    cfg=CodexSandboxConfig(auth_file=auth,persist_auth_updates=True) if live else None
    persona=Persona.load(ROOT/'scenarios/personas/curious.toml')
    with Store.open(source/'persona.db',readonly=True) as store:
        ctl=StateController(store)
        expected={'state_sha256':digest(ctl.state),'journal_sha256':digest([e.to_dict() for e in ctl.events_after(0)]),'seq':store.last_seq()}
    pause=[False];runs=[]
    cells=[('legacy',1,'diagnosis'),('fixed',1,'diagnosis'),('fixed',2,'diagnosis'),('legacy',2,'diagnosis'),
           ('fixed',1,'free'),('fixed',2,'free')]
    for profile,repeat,task in cells:
        code=f'{task}-{profile}-{repeat}'
        if pause[0]:
            runs.append({'code':code,'status':'not_run','reason':'provider_pause'});continue
        directory=output/code;fork_checkpoint(source,directory)
        providers=[]
        def factory(runtime):
            raw=CodexProvider(subprocess_runner,cfg,max_format_retries=0) if live else provider_factory(runtime,task)
            bounded=SessionBudget(raw,pause);providers.append(bounded);return bounded
        architecture=(ProvenanceArchitecture if profile=='fixed' else TransferArchitecture)(factory)
        decisions=[]
        with open_for_write(directory/'persona.db') as store:
            runtime=DevelopmentRuntime(store,persona,SWITCHES,calendar_ms=30*DAY)
            initial={'state_sha256':digest(runtime.controller.state),
                     'journal_sha256':digest([e.to_dict() for e in runtime.controller.events_after(0)]),'seq':store.last_seq()}
            assert initial==expected
            runtime.step({'kind':'session','action':'start'})
            before_seed=copy.deepcopy(runtime.controller.state)
            seed_history(runtime,source/'trace.json',sha,cutoff)
            after_seed=runtime.controller.state;after_seed['world_time']['world'].pop(REGISTRY)
            assert after_seed==before_seed, 'разметка изменила состояние личности'
            cycle=architecture.build(runtime)
            if task=='diagnosis':
                cycle.allowed_proposals=('no_action',)
                original=cycle._context
                def context(trigger,focus,recalled):
                    result=original(trigger,focus,recalled)
                    result[0]={**result[0],'text':result[0]['text']+' '+QUESTION};return result
                cycle._context=context
            for n in range(1 if task=='diagnosis' else 4):
                runtime.step({'kind':'wait','life_ms':60000})
                start=store.last_seq();before=runtime.controller.state
                report=cycle.run(runtime.trigger('provenance_'+task+'_'+str(n)))
                events=runtime.controller.events_after(start)
                decisions.append({'step':n,'before':before,'after':runtime.controller.state,'report':asdict(report),
                    'requests':[e.payload['request'] for e in events if e.type=='proposal.requested'],
                    'proposals':[e.payload for e in events if e.type=='proposal.received'],
                    'applied':[e.payload for e in events if e.changes_state]})
                if pause[0]:break
            runtime.step({'kind':'session','action':'stop'})
        result,problems=save_trace(directory,architecture,persona,decisions,[],SWITCHES)
        trial={'code':code,'profile':profile,'task':task,'repeat':repeat,'status':'executed','initial':initial,
               'decisions':len(decisions),'calls':sum(p.calls for p in providers),'integrity_problems':problems,
               'trace':code+'/trace.json','sha256':hashlib.sha256(result.trace_bytes).hexdigest()}
        runs.append(trial);(output/'progress.json').write_text(json.dumps(runs,ensure_ascii=False,indent=2)+'\n')
        print(code,'calls',trial['calls'],'outcomes',[d['report']['outcome'] for d in decisions],'errors',len(problems),flush=True)
    summary={'protocol':'provenance-v1','profile_version':PROFILE_VERSION,'live':live,'model':'gpt-6-luna' if live else 'scripted',
             'source':str(source.resolve()),'source_sha256':sha,'setup_seq':cutoff,'question':QUESTION,
             'provider_paused':pause[0],'runs':runs,'human_ratings':None,
             'limitation':'Диагностика понимания после вопроса и восемь свободных выборов, не проверка спонтанной цели или всей личности.'}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--manifest',required=True)
    p.add_argument('--output',required=True);p.add_argument('--auth-file',required=True)
    a=p.parse_args();run(a.source,a.manifest,a.output,a.auth_file)
