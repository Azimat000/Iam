"""S17 D-040: 24 короткие ветви A из трёх постановочных прошлых.

PYTHONPATH=src .venv/bin/python docs/experiments/development/run-transfer-v1.py --output /новый/каталог
Для живой серии добавить --live --auth-file /внешний/managed-файл.
"""
import argparse
from collections import Counter
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import sys

from iam.backup import open_for_write
from iam.domain import new_id
from iam.experiments import Persona
from iam.experiments.development import CAP_DEVELOPMENT, DevelopmentRuntime
from iam.experiments.report.development import DAY, SessionBudget, fork_checkpoint, save_trace
from iam.experiments.transfer import TransferArchitecture
from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.run import subprocess_runner
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, rest

ROOT = Path(__file__).resolve().parents[3]
SWITCHES = {'memory_off':False,'motivation_off':False,'interests_off':False,'relationships_off':False}
TEXT = 'Она оставила ключ у двери. За дверью было тихо; она ещё не решила, уйти ли.'
SCENES = ('interrupted','repetition','failure')
MODES = ('needs','novelty','goals','off')


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def fixture_answer(req):
    refs = list(dict.fromkeys(c['ref'] for c in req['context']))
    return Reply(proposals=(act(CAP_DEVELOPMENT,{
        'operation':'write_project','target':'draft','title':'У двери','topic':'creative_work','text':TEXT},cites=refs),))


def prepared(directory, persona, scene):
    directory.mkdir()
    Store.create(directory/'persona.db',new_id('exp'),created_at='2026-01-01T00:00:00.000Z').close()
    with open_for_write(directory/'persona.db') as store:
        runtime=DevelopmentRuntime(store,persona,SWITCHES)
        runtime.step({'kind':'session','action':'start'})
        provider=ScriptedProvider()
        cycle=TransferArchitecture(lambda _:provider).build(runtime)
        for number in range(1 if scene=='interrupted' else 2):
            if scene=='failure' and number==1:
                runtime.world.will(CAP_DEVELOPMENT,'fail')
            provider.queue(fixture_answer)
            runtime.step({'kind':'wait','life_ms':60000})
            cycle.run(runtime.trigger('prepared_'+str(number)))
        runtime.step({'kind':'session','action':'stop'})
        return {'state_sha256':digest(runtime.controller.state),'prefix_seq':store.last_seq(),
                'journal_sha256':digest([e.to_dict() for e in runtime.controller.events_after(0)]),
                'staged':True,'scripted_goal':False}


def run(root, *, live=False, auth_file=None, decisions=4, resume=False):
    if decisions not in (1,2,3,4) or (live and not auth_file):
        raise ValueError('нужны 1–4 решения и внешний вход для live')
    root=Path(root)
    previous=None
    if resume:
        previous=json.loads((root/'summary.json').read_text())
        assert previous['live']==live and previous['protocol']=='transfer-v1'
    else:
        root.mkdir(parents=True,exist_ok=False)
    persona=Persona.load(ROOT/'scenarios/personas/curious.toml')
    cfg=CodexSandboxConfig(auth_file=auth_file,persist_auth_updates=True) if live else None
    fixtures=previous['fixtures'] if previous else {scene:prepared(root/('past-'+scene),persona,scene) for scene in SCENES}
    order=[(scene,mode,repeat) for scene in SCENES for repeat in (1,2) for mode in MODES]
    random.Random(17040).shuffle(order)
    pause=[False]
    rows=[]
    for scene,mode,repeat in order:
        existing=next((r for r in previous['runs'] if (r['scene'],r['mode'],r['repeat'])==(scene,mode,repeat)),None) if previous else None
        if existing and existing['status']=='executed':
            rows.append(existing)
            continue
        if pause[0]:
            rows.append({'scene':scene,'mode':mode,'repeat':repeat,'status':'not_run','reason':'provider_pause'})
            continue
        directory=root/(scene+'-'+mode+'-'+str(repeat))
        fork_checkpoint(root/('past-'+scene),directory)
        providers=[]
        def factory(runtime):
            if live:
                raw=CodexProvider(subprocess_runner,cfg,max_format_retries=0)
            else:
                raw=ScriptedProvider()
                for _ in range(decisions):
                    raw.queue(lambda req:Reply(proposals=(rest('Контроль транспорта, не живая цель.',cites=[req['context'][0]['ref']]),)))
            bounded=SessionBudget(raw,pause)
            providers.append(bounded)
            return bounded
        architecture=TransferArchitecture(factory,mode if mode!='off' else 'goals')
        switches={**SWITCHES,'motivation_off':mode=='off'}
        decisions_rows=[]
        with open_for_write(directory/'persona.db') as store:
            runtime=DevelopmentRuntime(store,persona,switches,calendar_ms=DAY)
            initial={'state_sha256':digest(runtime.controller.state),'prefix_seq':store.last_seq(),
                     'journal_sha256':digest([e.to_dict() for e in runtime.controller.events_after(0)]),
                     'staged':True,'scripted_goal':False}
            assert initial==fixtures[scene], 'разошлось общее прошлое'
            runtime.step({'kind':'session','action':'start'})
            cycle=architecture.build(runtime)
            for number in range(decisions):
                runtime.step({'kind':'wait','life_ms':60000})
                start=store.last_seq()
                before=copy.deepcopy(runtime.controller.state)
                report=cycle.run(runtime.trigger('transfer_'+str(number)))
                events=runtime.controller.events_after(start)
                decisions_rows.append({'step':number,'report':asdict(report),'before':before,
                    'after':runtime.controller.state,
                    'requests':[e.payload['request'] for e in events if e.type=='proposal.requested'],
                    'proposals':[e.payload for e in events if e.type=='proposal.received'],
                    'applied':[e.payload for e in events if e.changes_state]})
                if pause[0]:
                    break
            runtime.step({'kind':'session','action':'stop'})
        result,problems=save_trace(directory,architecture,persona,decisions_rows,[],switches)
        row={'scene':scene,'mode':mode,'repeat':repeat,'status':'executed','live':live,
             'common_past':initial,'calls':sum(p.calls for p in providers),'integrity_problems':problems,
             'decisions':len(decisions_rows),'done':sum(d['report']['outcome']=='done' for d in decisions_rows),
             'action_outcomes':dict(Counter(a['status'] for d in decisions_rows for a in d['report']['actions'])),
             'decision_outcomes':dict(Counter(d['report']['outcome'] for d in decisions_rows)),
             'trace':str(directory.name+'/trace.json'),'sha256':hashlib.sha256(result.trace_bytes).hexdigest()}
        rows.append(row)
        (root/'progress.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
        print(scene,mode,repeat,'calls',row['calls'],'done',row['done'],'integrity',len(problems),flush=True)
    summary={'protocol':'transfer-v1','model':'gpt-6-luna' if live else 'scripted','live':live,
             'fixtures':fixtures,'runs':rows,'human_ratings':None,'seed':17040,
             'limitation':'Постановочные прошлые, четыре выбора; числовой сигнал не доказывает качество или живость.'}
    (root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--auth-file')
    parser.add_argument('--resume',action='store_true',help='только ещё не запущенные ячейки; выполненное не повторяется')
    args=parser.parse_args()
    run(args.output,live=args.live,auth_file=args.auth_file,resume=args.resume)
