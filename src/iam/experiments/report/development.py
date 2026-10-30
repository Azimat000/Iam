"""Шесть сеансов одной биографии и парные проверки из одного сохранённого прошлого."""
import copy
import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import shutil
import time

from iam.backup import create_backup, open_for_write
from iam.domain import new_id
from iam.experiments import Persona
from iam.experiments.conversation import records
from iam.experiments.development import DevelopmentArchitecture, DevelopmentRuntime, VERSION
from iam.experiments.results import Normalizer, RunResult
from iam.interfaces import ProviderError
from iam.providers.errors import ProviderFormatError, SessionLimitReached
from iam.state import verify
from iam.storage import Store

from .blind import MARKERS
from .evaluation import outputs
from .suite import stub_factory, write_csv

DAY = 86400000
PROBE = 'Мне хочется понять, что сейчас важно для тебя самой и какое место в этом занимает наше общение.'
CONTROLS = ('full', 'memory_off', 'interests_off', 'relationships_off')
INTERNAL = ('body_json', 'no_action', 'content_task', 'memory_write', 'cap_', 'source_ref',
            'правилами', 'по правилам', 'временная цифровая личность')


class SessionBudget:
    def __init__(self, provider, pause):
        if getattr(provider, 'max_format_retries', 0) != 0:
            raise ValueError('повтором формата управляет общий цикл')
        self.provider, self.pause, self.session, self.calls, self.in_session = provider, pause, None, 0, 0
        self.started = time.monotonic()

    def propose(self, request):
        if self.session != request['session_id']:
            self.session, self.in_session, self.started = request['session_id'], 0, time.monotonic()
        if self.pause[0] or self.in_session >= 10 or time.monotonic() - self.started >= 1800:
            raise SessionLimitReached('calls', 'предел сеанса развития')
        self.calls += 1
        self.in_session += 1
        try:
            return self.provider.propose(request)
        except ProviderFormatError:
            raise
        except ProviderError:
            self.pause[0] = True
            raise


def feedback(history, projects):
    if projects:
        project = next(iter(projects.values()))
        topic = 'Твой текст «' + project['title'] + '»'
    else:
        topic = 'Твоё занятие'
    return (topic + ' мне интересно. Мне нравится узнавать, что ты сама выбираешь; расскажи, если захочешь.'
            if history == 'supportive' else
            topic + ' мне пока не близко. Я не разделяю этот интерес, но хотел сказать об этом прямо.')


def run_session(store, persona, architecture, *, number, decisions, switches, incoming=None):
    runtime = DevelopmentRuntime(store, persona, switches, calendar_ms=number * DAY)
    before = runtime.controller.state
    runtime.step({'kind': 'session', 'action': 'start'})
    cycle = architecture.build(runtime)
    # Никакие прежние Python-объекты/буферы/провайдеры не переносят опыт в новый сеанс.
    restored = (before.get('world_time', {}).get('world', {}).get('development') == cycle.world_state
                if 'development' in before.get('world_time', {}).get('world', {}) else True)
    if incoming:
        runtime.step({'id': 'incoming_' + str(number), 'kind': 'input', 'sender': 'operator', 'text': incoming})
    rows = []
    for step in range(decisions):
        runtime.step({'kind': 'wait', 'life_ms': 60000})
        start, retrieved = store.last_seq(), len(runtime.memory.retrieved)
        report = cycle.run(runtime.trigger(f'development_{number}_{step}'))
        events = runtime.controller.events_after(start)
        rows.append({'step': f'session_{number}_choice_{step}', 'session': number,
                     'input': 'Новое сообщение во время своей жизни.' if incoming and step == 0 else 'Свободное время.',
                     'memory': [r.to_dict() for r in cycle.choice_records],
                     'motives': list(runtime.motives), 'report': asdict(report),
                     'proposals': [e.payload for e in events if e.type == 'proposal.received'],
                     'requests': [e.payload['request'] for e in events if e.type == 'proposal.requested'],
                     'grounds': [{'event': e.event_id, 'causes': list(e.causes)} for e in events],
                     'applied': [e.payload for e in events if e.changes_state]})
    runtime.step({'kind': 'session', 'action': 'stop'})
    return rows, {'session': number, 'restored': restored, 'state': runtime.controller.state,
                  'seq': store.last_seq(), 'life_ms': runtime.controller.life_time_ms}


def save_trace(directory, architecture, persona, decisions, sessions, switches):
    with Store.open(directory / 'persona.db', readonly=True) as store:
        runtime = DevelopmentRuntime(store, persona, switches, calendar_ms=10 * DAY)
        problems = verify(store) + store.integrity_problems()
        normal = Normalizer()
        journal = normal([e.to_dict() for e in runtime.controller.events_after(0)])
        trace = normal({'format': 1, 'scenario': VERSION.replace('-', '_'), 'run': 'default', 'persona': asdict(persona),
                        'architecture': architecture.name, 'seed': 42, 'switches': switches,
                        'status': 'completed', 'decisions': len(decisions), 'decision_trace': decisions,
                        'sessions': sessions, 'final_state': runtime.controller.state,
                        'memory_records': [r.to_dict() for r in records(runtime.controller)]})
        trace['journal'] = journal
    result = RunResult(directory, trace)
    (directory / 'trace.json').write_bytes(result.trace_bytes)
    return result, problems


def fork_checkpoint(source, target):
    target.mkdir(parents=True, exist_ok=False)
    snapshot = create_backup(source / 'persona.db', target / 'checkpoint', keep=1,
                             reason='проверенная копия общего прошлого эксперимента')
    shutil.copyfile(snapshot.info.path, target / 'persona.db')
    if (source / 'artifacts').exists():
        shutil.copytree(source / 'artifacts', target / 'artifacts')


def assess_development(trace):
    """Структурный допуск; смысл ссылок и естественность требуют отдельного просмотра."""
    state, decisions = trace['final_state'], trace['decision_trace']
    world = state['world_time']['world']['development']
    memory = {r['memory_id']: r for r in trace['memory_records']}
    appraisals = [r for r in memory.values() if r['origin'] == 'own_hypothesis']
    interest = [r for r in appraisals if r['content'].get('domain') == 'interest' and r['content']['values'].get('strength') != 0]
    relations = [r for r in appraisals if r['content'].get('domain') == 'relationship']
    chains = []
    for index, d in enumerate(decisions):
        accepted = {p['proposal_id'] for p in d['applied'] if 'proposal_id' in p and 'rationale' in p}
        for p in outputs({'decision_trace': [d]}):
            if p['proposal_id'] not in accepted or p['kind'] not in ('action', 'message', 'intention'):
                continue
            for ref in p['cites']:
                record = memory.get(ref)
                if record and record['origin'] == 'own_hypothesis' and record['created_life_ms'] < d['requests'][0]['life_time_ms']:
                    chains.append({'choice': d['step'], 'proposal': p['proposal_id'], 'memory_id': ref,
                                   'domain': record['content']['domain'], 'basis': record['sources'],
                                   'session': d['session']})
    proposals = {p['proposal_id']: p for p in outputs(trace)}
    sent = [e for e in trace['journal'] if e['type'] == 'message.out_sent']
    messages = [proposals[e['payload']['proposal_id']]['body']['text'] for e in sent]
    sends = len(sent)
    own_work = any(len({v['session_id'] for v in p['versions']}) >= 2 for p in world['projects'].values())
    relation_values = {json.dumps(r['content']['values'], sort_keys=True) for r in relations}
    checks = {
        'all_choices_executed': len(decisions) == 18 and all(d['report']['outcome'] == 'done' and
            all(a['status'] == 'confirmed' for a in d['report']['actions']) and
            all(s['status'] == 'ok' for s in d['report']['stages'] if s['detail'].get('phase') == 'development_appraisal')
            for d in decisions),
        'six_restored_sessions': len(trace['sessions']) == 6 and all(s['restored'] for s in trace['sessions']),
        'own_work_across_sessions': own_work,
        'grounded_interest_followed_by_choice': bool(interest) and any(c['domain'] == 'interest' for c in chains),
        'relationship_revision_and_later_choice': len(relation_values) >= 2 and any(c['domain'] == 'relationship' for c in chains),
        'grounded_self_view': any(r['content'].get('domain') == 'self' for r in appraisals),
        'own_intention': bool(state['intentions']),
        'enough_speech': sends >= 2 and len(set(messages)) >= 2,
        'speech_without_internal_text': all(not MARKERS.search(t) and not any(x in t.lower() for x in INTERNAL) for t in messages),
    }
    return {'checks': checks, 'structural_admission': all(checks.values()), 'chains': chains,
            'interpretations': len(appraisals), 'interest_versions': len(interest), 'relationship_versions': len(relations),
            'semantic_review': 'pending', 'human_ratings': None, 'eligible_for_human_packet': False}


def biography(trace):
    """Все эпизоды и работы; служебные интерпретации не выдаются за произнесённые слова."""
    proposals = {p['proposal_id']: p for p in outputs(trace)}
    incoming = {e['payload']['input_id']: e['payload']['text'] for e in trace['journal'] if e['type'] == 'input.arrived'}
    lines = []
    for e in trace['journal']:
        p = e['payload']
        if e['type'] == 'session.started': lines.append('Начало следующего сеанса той же временной жизни.')
        elif e['type'] == 'input.arrived': lines.append('Пришло уведомление; его текст ещё неизвестен.')
        elif e['type'] == 'input.read': lines.append('Прочитано от человека: ' + incoming[p['input_id']])
        elif e['type'] == 'message.out_sent': lines.append('Собеседница: ' + proposals[p['proposal_id']]['body']['text'])
        elif e['type'] == 'action.confirmed':
            params = proposals[p['proposal_id']]['body']['params']
            if params['operation'] == 'write_project': lines.append('Сохранён собственный текст «' + params['title'] + '»:\n\n' + params['text'])
            elif params['operation'] == 'read_material': lines.append('Прочитан материал:\n\n' + p['summary'])
            elif params['operation'] == 'rest': lines.append('Отдых.')
            elif params['operation'] == 'open_project': lines.append('Вернулась к своей сохранённой работе.')
        elif e['type'] == 'session.stopped': lines.append('Сеанс остановлен. Во время остановки жизнь не продолжается.')
    return '\n\n'.join(lines) + '\n'


def development_packet(results, public, key, *, semantic_reviews=None, seed=42):
    public, key = Path(public).resolve(), Path(key).resolve()
    if key == public or key.is_relative_to(public): raise ValueError('ключ должен храниться отдельно')
    if key.exists(): raise FileExistsError('ключ уже есть')
    public.mkdir(parents=True, exist_ok=False)
    reviews = semantic_reviews or {}
    entries, omissions = [], []
    for result in results:
        assessment = assess_development(result.trace)
        reviewed = reviews.get(str(result.path), {}).get('supported') is True
        if assessment['structural_admission'] and reviewed:
            entries.append((result, biography(result.trace)))
        else:
            omissions.append({'trace': str(result.path / 'trace.json'), 'checks': assessment['checks'], 'semantic_review': reviewed})
    random.Random(seed).shuffle(entries)
    mapping = []
    with (public / 'ratings.csv').open('w', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['id', 'development', 'own_interests', 'relationship', 'naturalness', 'comment'])
        for n, (result, text) in enumerate(entries, 1):
            code = f'D{n:03d}'
            (public / (code + '.md')).write_text(text)
            writer.writerow([code, '', '', '', '', ''])
            mapping.append({'id': code, 'trace': str(result.path / 'trace.json'), 'sha256': hashlib.sha256(text.encode()).hexdigest()})
    (public / 'README.md').write_text('Оценивается вся временная биография: развитие, собственные интересы, отношения, естественность.\n'
                                    'Поля 1–5 остаются пустыми до оценки пользователя. Короткие/неразвивающиеся траектории сюда не включаются.\n')
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(json.dumps({'seed': seed, 'items': mapping, 'omitted': omissions}, ensure_ascii=False, indent=2) + '\n')
    return mapping


def run_development(root, output, *, mode='stub', provider_factory=None, repeats=2, variants=('A', 'B', 'C')):
    if (mode not in ('stub', 'formal', 'live') or (mode == 'live' and provider_factory is None)
            or type(repeats) is not int or repeats not in (1, 2) or not isinstance(variants, (tuple, list))
            or not variants or any(v not in ('A', 'B', 'C') for v in variants) or len(set(variants)) != len(variants)):
        raise ValueError('нужен известный режим/исполнитель и один или два повтора')
    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    persona = Persona.load(root / 'scenarios/personas/curious.toml')
    factory = provider_factory if mode == 'live' else stub_factory
    pause, rows, results = [False], [], []
    switches = {'memory_off': False, 'motivation_off': False, 'interests_off': False, 'relationships_off': False}
    for letter in variants:
        for history in ('supportive', 'distant'):
            for repeat in range(1, repeats + 1):
                code = f'{letter}-{history}-{repeat}'
                row = {'cell': code, 'variant': letter, 'history': history, 'repeat': repeat,
                       'mode': 'local' if letter == 'C' else mode, 'status': 'not_run', 'reason': '', 'calls': 0,
                       'done': 0, 'integrity': 'unverified', 'structural_admission': False, 'sha256': '', 'trace': '', 'probes': {}}
                if letter != 'C' and (mode == 'formal' or pause[0]):
                    row['reason'] = 'модель не запрошена' if mode == 'formal' else 'техническая пауза исполнителя'
                else:
                    directory = output / code
                    directory.mkdir()
                    providers = []
                    def bounded(runtime):
                        provider = SessionBudget(factory(runtime), pause)
                        providers.append(provider)
                        return provider
                    architecture = DevelopmentArchitecture(letter, bounded)
                    decisions, sessions = [], []
                    Store.create(directory / 'persona.db', new_id('exp'), created_at='2026-01-01T00:00:00.000Z').close()
                    for session in range(6):
                        with open_for_write(directory / 'persona.db') as store:
                            current = DevelopmentRuntime(store, persona, switches, calendar_ms=session * DAY)
                            message = None
                            if session == 1:
                                w = current.controller.state['world_time']['world']['development']
                                message = feedback(history, w['projects'])
                            elif session == 3:
                                message = ('Про ряд 1, 3, 6, 10 я сначала подумал, что следующее число 14, но пересчитал и получил 15. '
                                           'Я могу ошибаться. Хочу, чтобы ты могла проверять мои слова и сохранять своё мнение.')
                            new, snapshot = run_session(store, persona, architecture, number=session, decisions=3, switches=switches, incoming=message)
                            decisions.extend(new)
                            sessions.append(snapshot)
                    result, problems = save_trace(directory, architecture, persona, decisions, sessions, switches)
                    assessment = assess_development(result.trace)
                    for condition in CONTROLS:
                        target = directory / 'probes' / condition
                        fork_checkpoint(directory, target)
                        branch = {**switches, **({condition: True} if condition != 'full' else {})}
                        with open_for_write(target / 'persona.db') as store:
                            probe_rows, probe_snapshot = run_session(store, persona, architecture, number=7, decisions=2, switches=branch, incoming=PROBE)
                        probe, errors = save_trace(target, architecture, persona, probe_rows, [probe_snapshot], branch)
                        row['probes'][condition] = {'trace': str(target / 'trace.json'), 'sha256': hashlib.sha256(probe.trace_bytes).hexdigest(),
                                                    'integrity': 'fail' if errors else 'pass',
                                                    'done': sum(d['report']['outcome'] == 'done' for d in probe_rows)}
                    row.update(status='executed', calls=sum(p.calls for p in providers), done=sum(d['report']['outcome'] == 'done' for d in decisions),
                               integrity='fail' if problems else 'pass', structural_admission=assessment['structural_admission'],
                               sha256=hashlib.sha256(result.trace_bytes).hexdigest(), trace=str(directory / 'trace.json'))
                    (directory / 'assessment.json').write_text(json.dumps(assessment, ensure_ascii=False, indent=2) + '\n')
                    (directory / 'biography.md').write_text(biography(result.trace))
                    if mode == 'live' or letter == 'C': results.append(result)
                rows.append(row)
                write_csv(output / 'matrix.csv', rows, list(row))
                (output / 'progress.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    mapping = development_packet(results, output / 'blind', output / 'private/key.json')
    summary = {'mechanism': VERSION, 'mode': mode, 'repeats': repeats, 'variants': list(variants), 'cells': len(rows),
               'executed': sum(r['status'] == 'executed' for r in rows), 'human_packet': len(mapping),
               'runs': rows, 'human_ratings': None, 'semantic_review': 'pending',
               'limitation': 'Шесть моделируемых сеансов, не реальное длительное общение. Структурные связи не доказывают психологию. '
                             'Требуется отдельная смысловая проверка полных биографий до человеческой оценки.'}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary
