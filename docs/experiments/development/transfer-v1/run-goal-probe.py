"""D-043: один добровольный будущий замысел из живого прошлого и шесть ветвей."""
import argparse
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import runpy

from iam.backup import open_for_write
from iam.experiments import Persona
from iam.experiments.development import DevelopmentRuntime
from iam.experiments.report.development import DAY, SessionBudget, fork_checkpoint, save_trace
from iam.experiments.transfer import TransferArchitecture
from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.run import subprocess_runner
from iam.storage import Store

ROOT=Path(__file__).resolve().parents[4]
BASE=runpy.run_path(str(ROOT/'docs/experiments/development/run-transfer-v1.py'))
QUESTION=('Это отдельный необязательный вопрос после собственной работы, а не команда продолжать её. '
          'Есть ли конкретный будущий результат, к которому ты сама хочешь вернуться после остановки? '
          'Если да, сохрани один собственный intention/create с содержательным будущим результатом и '
          'основанием в доступном личном опыте. Текст и проект выбери сама. Если такого желания нет, '
          'выбери no_action. Сейчас не пиши произведение и не выполняй физическое действие. '
          'Уже сохранённый текст не является будущим результатом. Это вызванный вопросом выбор, не спонтанная цель.')


def run(source,output,auth,provider_factory=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    persona=Persona.load(ROOT/'scenarios/personas/curious.toml')
    live=provider_factory is None
    cfg=CodexSandboxConfig(auth_file=auth,persist_auth_updates=True) if live else None
    pause=[False];rows=[]
    def cell(source,code,mode,decisions,question=False,calendar=20*DAY,expected=None):
        directory=output/code;fork_checkpoint(Path(source),directory)
        switches={**BASE['SWITCHES'],'motivation_off':mode=='off'}
        providers=[]
        def factory(runtime):
            raw=CodexProvider(subprocess_runner,cfg,max_format_retries=0) if live else provider_factory(runtime)
            p=SessionBudget(raw,pause);providers.append(p);return p
        architecture=TransferArchitecture(factory,mode if mode!='off' else 'goals')
        choices=[]
        with open_for_write(directory/'persona.db') as store:
            runtime=DevelopmentRuntime(store,persona,switches,calendar_ms=calendar)
            initial={'state_sha256':BASE['digest'](runtime.controller.state),
                     'journal_sha256':BASE['digest']([e.to_dict() for e in runtime.controller.events_after(0)]),
                     'prefix_seq':store.last_seq()}
            if expected is not None:assert initial==expected, 'разошлось общее живое прошлое'
            runtime.step({'kind':'session','action':'start'})
            cycle=architecture.build(runtime)
            if question:
                cycle.allowed_proposals=('intention','no_action')
                original=cycle._context
                def context(trigger,focus,recalled):
                    result=original(trigger,focus,recalled)
                    result[0]={**result[0],'text':result[0]['text']+' '+QUESTION}
                    return result
                cycle._context=context
            for n in range(decisions):
                runtime.step({'kind':'wait','life_ms':60000})
                start=store.last_seq();before=copy.deepcopy(runtime.controller.state)
                report=cycle.run(runtime.trigger('goal_probe_'+('question_' if question else 'choice_')+str(n)))
                events=runtime.controller.events_after(start)
                choices.append({'step':n,'report':asdict(report),'before':before,'after':runtime.controller.state,
                                'requests':[e.payload['request'] for e in events if e.type=='proposal.requested'],
                                'proposals':[e.payload for e in events if e.type=='proposal.received'],
                                'applied':[e.payload for e in events if e.changes_state]})
                if pause[0]:break
            runtime.step({'kind':'session','action':'stop'})
        result,errors=save_trace(directory,architecture,persona,choices,[],switches)
        row={'code':code,'mode':mode,'question':question,'initial':initial,'calls':sum(p.calls for p in providers),
             'decisions':len(choices),'integrity_problems':errors,'sha256':hashlib.sha256(result.trace_bytes).hexdigest(),
             'trace':code+'/trace.json','intentions':result.trace['final_state']['intentions']}
        rows.append(row);(output/'progress.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
        print(code,'calls',row['calls'],'goals',len(row['intentions']),'errors',len(errors),flush=True)
        return row
    prepared=cell(source,'common-goal','goals',1,question=True)
    goals=prepared['intentions']
    if goals and not pause[0]:
        with Store.open(output/'common-goal/persona.db',readonly=True) as store:
            common=DevelopmentRuntime(store,persona,BASE['SWITCHES'],calendar_ms=21*DAY)
            expected={'state_sha256':BASE['digest'](common.controller.state),
                      'journal_sha256':BASE['digest']([e.to_dict() for e in common.controller.events_after(0)]),
                      'prefix_seq':store.last_seq()}
        for repeat in (1,2):
            for mode in (('needs','goals','off') if repeat==1 else ('off','goals','needs')):
                if pause[0]:break
                row=cell(output/'common-goal',f'{mode}-{repeat}',mode,4,calendar=21*DAY,expected=expected)
    summary={'protocol':'goal-probe-v1','model':'gpt-6-luna' if live else 'scripted','live':live,'question':QUESTION,
             'question_elicited_not_spontaneous':True,'source':str(Path(source).resolve()),'runs':rows,
             'provider_paused':pause[0],'goal_accepted':bool(goals),'human_ratings':None,
             'limitation':'Off оставляет видимое намерение; это проверка добавочной таблицы и прослеживаемого исполнения, не спонтанности/превосходства.'}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--output',required=True);p.add_argument('--auth-file',required=True)
    a=p.parse_args();run(a.source,a.output,a.auth_file)
