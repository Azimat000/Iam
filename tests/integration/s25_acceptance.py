"""D-097. Five fresh histories and fixed controls from their stopped checkpoints."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from iam.storage import Store

from integration.goal_series import artifacts, load
from integration.intention_probe import snapshot
from integration.live_series import MODEL, ROOT, StopSeries, initial_database, sha
from integration.project_affordance import IMAGE, check_frozen, events, guard_store, next_session, run_session, sources

DESIGN = ROOT / 'docs/evidence/S25-pending-work-design.md'
PUBLICATIONS = {
    'greenhouse': (
        ('Теплица. Утренняя запись',
         'В 8:00 на стенке закрытой теплицы крупные капли воды. Воздух 16 °C, влажность 91%. Рядом на улице 10 °C. В 11:00 форточку открыли на двадцать минут. Второй замер ведёт сменившийся наблюдатель, его журнал появится в библиотеке только в следующем сеансе.'),
        ('Теплица. Запись после проветривания',
         'В 11:20 в той же точке внутри теплицы 18 °C и влажность 74%. Капли на стенке ещё видны, но их стало меньше. Снаружи 15 °C. Солнце вышло в 10:50. Другую теплицу с закрытой форточкой не измеряли.'),
    ),
    'timetable': (
        ('Архив поездок. Понедельник',
         'Рейс 14 отправился в 9:05 и прибыл в 9:43. В старом расписании указан путь за 32 минуты. Дежурный записал пятиминутную остановку у моста. Вторая ведомость по той же линии ещё в пути, её опубликуют только в следующем сеансе.'),
        ('Архив поездок. Вторник',
         'Рейс 14 отправился в 9:02 и прибыл в 9:35. У моста сегодня не останавливался. Ремонтная бригада работала на другом участке линии. Путевое время в новом расписании не меняли. О двух днях с праздниками сведений нет.'),
    ),
    'window': (
        ('Свет у окна. Первая запись',
         'У северного окна на расстоянии полметра от стекла в 10:00 измерено 620 люкс, на расстоянии двух метров — 210 люкс. Небо облачное. Журнал такого же замера у южного окна будет доступен только в следующем сеансе.'),
        ('Свет у окна. Вторая запись',
         'У южного окна в 10:00 в полуметре от стекла измерено 970 люкс, в двух метрах — 290 люкс. Небо облачное. Занавеска у южного окна открыта; степень открытия северной занавески не записана.'),
    ),
    'shore': (
        ('Берег. Отметка до ветра',
         'У деревянного причала в 7:15 уровень воды был на 11 сантиметров ниже красной метки. Ветер почти отсутствовал. У камней виднелась широкая сухая полоса. Следующая отметка с того же столба появится в библиотеке только в следующем сеансе.'),
        ('Берег. Отметка после ветра',
         'У того же причала в 18:40 вода стояла на 4 сантиметра выше красной метки. Ветер с озера усилился, а над верховьями прошёл дождь. Камни у столба мокрые. Наблюдение на противоположном берегу отсутствует.'),
    ),
    'cloth': (
        ('Мастерская. Первая проба ткани',
         'У двух полосок хлопка из одной партии отметили длину 20 сантиметров. После сушки первой полоски на воздухе длина стала 19,8 сантиметра. Вторая полоска ещё сохнет у нагревателя, запись о ней появится в библиотеке только в следующем сеансе.'),
        ('Мастерская. Вторая проба ткани',
         'Полоску у нагревателя измерили после остывания: 19,2 сантиметра. Температуру у нагревателя не измеряли. Обе полоски перед сушкой вымачивались десять минут, но первую при измерении слегка натянули. Других полосок не было.'),
    ),
}
CONTROLS = (('greenhouse', 'memory'), ('timetable', 'motivation'), ('window', 'memory'))
TOTAL_CALLS = 216


def initial_record(path):
    with Store.open(path, readonly=True) as store:
        guard_store(store)
        state = store.latest_snapshot().state
        if state['world_time']['session_state'] != 'stopped':
            raise StopSeries('unstopped_acceptance_source')
        return dict(sha256=sha(path), head=events(store)[-1],
                    materials=list(state['world_time']['world'].get('library', {})))


def prepare_primary(root):
    initial = {}
    for branch in PUBLICATIONS:
        path = root / f'initial/{branch}.db'
        initial_database(path, branch)
        initial[branch] = initial_record(path)
        snapshot(path, root / f'stories/{branch}/persona.db')
    return initial


def prepare_controls(root, primary, summary):
    initial, switches, hashes, missing = {}, {}, {}, []
    for story, switch in CONTROLS:
        checkpoint = next((r for r in summary['sessions'] if r['branch'] == story and r['number'] == 2), {})
        if not checkpoint.get('checkpoint_sha256'):
            missing.append(story)
            continue
        relative = f'checkpoints/{story}.db'
        origin = primary / relative
        source = initial_record(origin)
        if source['sha256'] != checkpoint['checkpoint_sha256'] or source['head'] != checkpoint['head']:
            raise StopSeries('control_checkpoint_changed')
        hashes[relative] = source['sha256']
        for repeat in (1, 2):
            for mode in ('full', switch):
                branch = f'{story}-{repeat}-{mode}'
                path = root / f'initial/{branch}.db'
                snapshot(origin, path)
                initial[branch] = initial_record(path)
                if initial[branch]['head'] != source['head']:
                    raise StopSeries('control_initial_history_changed')
                snapshot(path, root / f'stories/{branch}/persona.db')
                switches[branch] = mode
    return initial, switches, hashes, missing


def drive(root, source, initial, environment, auth, *, sessions, max_calls, **extra):
    output = artifacts(root, auth)
    branches = list(initial)
    manifest = dict(version='D-097', source=str(source), sources=sources(DESIGN),
                    design=str(DESIGN.relative_to(ROOT)), source_hashes={}, initial=initial, model=MODEL,
                    image=IMAGE, mode='live' if auth else 'stub', preflight=environment,
                    branches=branches, cycles=dict.fromkeys(branches, 'options'), sessions=sessions,
                    decisions_per_session=2, max_decisions=len(branches)*sessions*2, max_calls=max_calls,
                    session_call_cap=10, calls_per_decision=6, native_retries=0, cycle_retries=0)
    manifest.update(extra)
    output.write('manifest.json', manifest, exclusive=True)
    summary = dict(status='running', manifest_sha256=sha(root/'manifest.json'), decisions=0, attempts=[], sessions=[])
    output.write('summary.json', summary, exclusive=True)
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(str(ROOT/p) for p in ('src', 'tests')),
           'PYTHONDONTWRITEBYTECODE': '1'}
    while branches and (scheduled := next_session(summary['sessions'], branches, sessions)):
        branch, number = scheduled
        command = [sys.executable, '-m', 'integration.s25_acceptance', '--worker', branch, str(number),
                   '--output', str(root)]
        if auth:
            command += ['--auth-file', str(auth)]
        proc = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        summary = load(root, 'summary.json')
        row = summary['sessions'][-1] if summary['sessions'] else {}
        if (row.get('name') != f'{branch}-{number}' or row.get('status') not in ('completed', 'branch_stopped')
                or bool(proc.returncode) != (row['status'] == 'branch_stopped')):
            summary['status'] = 'stopped'
            break
    if summary['status'] == 'running':
        summary['status'] = ('completed' if branches and len(summary['sessions']) == len(branches)*sessions
                             and all(row['status'] == 'completed' for row in summary['sessions']) else 'partial')
    check_frozen(root, manifest, summary)
    output.write('summary.json', summary)
    return summary


def run(root, auth=None, preflight=None):
    root = root.resolve()
    auth = auth.expanduser().absolute() if auth else None
    if root.is_relative_to(ROOT) or (auth and auth.resolve().is_relative_to(root)):
        raise ValueError('external_evidence_required')
    environment = json.loads(preflight.read_text()) if auth else None
    if auth and (environment['image'] != IMAGE or environment['model_calls'] != 0
                 or not all(environment[key] is True for key in ('isolation_passed', 'schema_matches', 'launcher_matches'))):
        raise ValueError('preflight_failed')
    root.mkdir(mode=0o700)
    primary = root / 'primary'
    initial = prepare_primary(primary)
    releases = {f'{branch}-{n}': data for branch, records in PUBLICATIONS.items()
                for n, data in enumerate(records, 1)}
    main = drive(primary, root, initial, environment, auth, sessions=3, max_calls=150, releases=releases,
                 checkpoint_session=2, checkpoint_branches=[story for story, _ in CONTROLS])
    report = dict(primary_status=main['status'], primary_decisions=main['decisions'],
                  primary_calls=len(main['attempts']), controls_status='not_started')
    if main['status'] != 'stopped':
        controls = root / 'controls'
        initial, switches, hashes, missing = prepare_controls(controls, primary, main)
        controls.mkdir(exist_ok=True)
        hashes.update({name: sha(primary / name) for name in ('manifest.json', 'summary.json')})
        comparison = drive(controls, primary, initial, environment, auth, sessions=1,
                           max_calls=min(120, TOTAL_CALLS-len(main['attempts'])),
                           switches=switches, source_hashes=hashes, missing_checkpoints=missing)
        status = 'partial' if missing and comparison['status'] == 'completed' else comparison['status']
        report.update(controls_status=status, controls_decisions=comparison['decisions'],
                      controls_calls=len(comparison['attempts']), missing_checkpoints=missing)
    artifacts(root, auth).write('result.json', report, exclusive=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--auth-file', type=Path)
    parser.add_argument('--preflight', type=Path)
    parser.add_argument('--worker', nargs=2)
    args = parser.parse_args()
    if args.worker:
        branch, number = args.worker
        return run_session(args.output, branch, int(number), args.auth_file)
    if args.auth_file and not args.preflight:
        parser.error('live requires preflight')
    result = run(args.output, args.auth_file, args.preflight)
    print(json.dumps(result, ensure_ascii=False))
    return int(result['primary_status'] != 'completed' or result['controls_status'] != 'completed')


if __name__ == '__main__':
    raise SystemExit(main())
