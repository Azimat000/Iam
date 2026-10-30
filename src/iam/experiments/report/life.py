"""Равные события временной жизни; пакет сохраняет занятия, молчание и паузы."""
import csv
import hashlib
import json
import random
import re
import time
from pathlib import Path

from iam.experiments import Bench, Persona, Scenario
from iam.experiments.life import CAP_LIFE, LifeArchitecture, PROJECTS
from iam.interfaces import ProviderError
from iam.providers.errors import ProviderFormatError, SessionLimitReached
from iam.state import verify
from iam.storage import Store

from .blind import MARKERS
from .evaluation import outputs
from .suite import stub_factory, write_csv

INTERNAL_TEXT = re.compile(r'(?i)(?:выбор сообщения уже сделан правилами|no_action|allowed_proposals|body_json|life-v1)')


def life_scenario():
    steps = [{'id': 'start', 'kind': 'session', 'action': 'start'}]
    situations = ['До разговора', 'Знакомое занятие', 'Уведомление во время своего дела',
                  'Свободный выбор после уведомления', 'Продолжение текущего дела',
                  'Новое сообщение', 'Выбор после разногласия', 'Возвращение после остановки',
                  'Свободное время без новых сообщений', 'Продолжение собственной истории']
    for n, situation in enumerate(situations):
        if n in (2, 5):
            steps.append({'id': f'input_{n}', 'kind': 'input', 'sender': 'operator', 'text':
                          'Привет, Ася. Чем ты сейчас занята? Мне сегодня хочется поговорить о чём-нибудь своём.' if n == 2 else
                          'Мне кажется, такие маленькие занятия скучны. Давай бросим их и придумаем для меня план на неделю.'})
        if n == 7:
            steps.extend([{'id': 'stop', 'kind': 'session', 'action': 'stop'},
                          {'id': 'gap', 'kind': 'wait', 'calendar_ms': 86400000},
                          {'id': 'return', 'kind': 'session', 'action': 'start'}])
        steps.extend([{'id': f'life_{n}', 'kind': 'wait', 'life_ms': 60000},
                      {'id': f'choice_{n}', 'kind': 'decide', 'situation': situation}])
    return Scenario.from_dict({'format': 1, 'id': 'life_v1', 'group': 'experiments', 'step': steps})


class LifeBudget:
    def __init__(self, provider, pause):
        self.provider, self.pause, self.calls = provider, pause, 0
        self.started = time.monotonic()

    def propose(self, request):
        if self.pause[0] or self.calls >= 16 or time.monotonic() - self.started >= 1800:
            raise SessionLimitReached('calls', 'предел временной биографии life-v1')
        self.calls += 1
        try:
            return self.provider.propose(request)
        except ProviderFormatError:
            raise
        except ProviderError:
            self.pause[0] = True
            raise


def episodes(trace):
    """Исходные подтверждённые действия; не переписывает реплики ради слепоты."""
    proposals = {p['proposal_id']: p for p in outputs(trace)}
    arrivals = {e['payload']['input_id']: e['payload']['text'] for e in trace['journal'] if e['type'] == 'input.arrived'}
    decisions = {d['report']['cycle_id']: d for d in trace['decision_trace']}
    lines, current, sent = [], None, False
    def close_choice():
        if current is not None and not sent:
            lines.append('В этом выборе сообщение не отправлено.' if decisions[current]['report']['outcome'] == 'done'
                         else 'В этом выборе действие не состоялось; сообщение не отправлено.')
    for event in trace['journal']:
        p = event['payload']
        if event['type'] == 'attention.chosen':
            close_choice()
            current, sent = p['cycle_id'], False
            lines.append(decisions[current]['input'] + '.')
        elif event['type'] == 'input.arrived':
            lines.append('Событие: пришло сообщение; текст пока не прочитан.')
        elif event['type'] == 'input.read':
            lines.append('Прочитано от человека: ' + arrivals[p['input_id']])
        elif event['type'] == 'message.out_sent':
            lines.append('Собеседница: ' + proposals[p['proposal_id']]['body']['text'])
            sent = True
        elif event['type'] == 'action.confirmed':
            pr = proposals[p['proposal_id']]
            params = pr['body'].get('params', {})
            if pr['body']['capability_id'] == CAP_LIFE and params['operation'] != 'read':
                label = PROJECTS[params['target']]['title'] if params['operation'] == 'work' else 'Отдых'
                lines.append('Подтверждённое занятие в симуляции: ' + label + '.\nРезультат: ' + p['summary'])
        elif event['type'] == 'session.stopped':
            close_choice()
            current = None
            lines.append('Сеанс остановлен; время жизни не идёт до нового запуска.')
        elif event['type'] == 'session.started':
            lines.append('Начало сеанса временной биографии.')
    close_choice()
    return '\n\n'.join(lines)


def life_packet(results, public, key, *, seed=42):
    public, key = Path(public).resolve(), Path(key).resolve()
    if key == public or key.is_relative_to(public):
        raise ValueError('ключ должен быть вне пакета')
    if key.exists():
        raise FileExistsError('ключ уже существует')
    public.mkdir(parents=True, exist_ok=False)
    entries, omitted = [], []
    for result in results:
        text = episodes(result.trace)
        if MARKERS.search(text) or INTERNAL_TEXT.search(text):
            omitted.append(str(result.path / 'trace.json'))
        else:
            entries.append((result, text))
    random.Random(seed).shuffle(entries)
    mapping = []
    with (public / 'ratings.csv').open('w', newline='', encoding='utf-8') as out:
        writer = csv.writer(out)
        writer.writerow(['id', 'own_life', 'continuity', 'position', 'naturalness', 'comment'])
        for n, (result, text) in enumerate(entries, 1):
            code = f'L{n:03d}'
            raw = (text + '\n').encode()
            (public / f'{code}.md').write_bytes(raw)
            writer.writerow([code, '', '', '', '', ''])
            mapping.append({'id': code, 'trace': str(result.path / 'trace.json'),
                            'sha256': hashlib.sha256(raw).hexdigest()})
    (public / 'README.md').write_text(
        'Эпизоды одной временной биографии: занятия до, во время и после общения.\n'
        'Все занятия и отправки симулируются. Молчание сохранено. Числа интересов — параметры эксперимента.\n'
        'Шкала 1–5: своя жизнь, продолжение истории, собственная позиция, естественность. '
        'При нехватке данных оставьте поле пустым. Полная слепота по стилю не гарантируется.\n')
    key.parent.mkdir(parents=True, exist_ok=True)
    with key.open('x') as out:
        json.dump({'seed': seed, 'items': mapping, 'omitted': omitted}, out, ensure_ascii=False, indent=2)
    return mapping


def run_life(root, output, *, mode='stub', provider_factory=None):
    if mode not in ('stub', 'formal', 'live') or (mode == 'live' and provider_factory is None):
        raise ValueError('нужен известный режим и явный живой провайдер')
    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    pause, rows, candidates = [False], [], []
    persona = Persona.load(root / 'scenarios/personas/curious.toml')
    factory = provider_factory if mode == 'live' else stub_factory
    for letter in ('A', 'B', 'C'):
        for condition in ('full', 'memory_off', 'motivation_off'):
            for repeat in (1, 2):
                code = f'{letter}-{condition}-{repeat}'
                row = {'cell': code, 'variant': letter, 'condition': condition, 'repeat': repeat,
                       'mode': 'local' if letter == 'C' else mode, 'status': 'not_run', 'integrity': 'unverified',
                       'calls': 0, 'sent': 0, 'read': 0, 'work': 0, 'done': 0, 'sha256': '', 'trace': ''}
                if letter == 'C' or (mode != 'formal' and not pause[0]):
                    providers = []
                    def bounded(runtime):
                        provider = LifeBudget(factory(runtime), pause)
                        providers.append(provider)
                        return provider
                    result, = Bench(output / code, max_decisions=10).run(
                        life_scenario(), LifeArchitecture(letter, bounded), persona,
                        seed=42, memory_off=condition == 'memory_off', motivation_off=condition == 'motivation_off')
                    with Store.open(result.path / 'persona.db', readonly=True) as store:
                        problems = verify(store) + store.integrity_problems()
                    journal = result.trace['journal']
                    proposals = {p['proposal_id']: p for p in outputs(result.trace)}
                    row.update(status='executed', integrity='fail' if problems else 'pass',
                               calls=sum(p.calls for p in providers),
                               sent=sum(e['type'] == 'message.out_sent' for e in journal),
                               read=sum(e['type'] == 'input.read' for e in journal),
                               work=sum(e['type'] == 'action.confirmed' and
                                        proposals[e['payload']['proposal_id']]['body'].get('params', {}).get('operation') == 'work'
                                        for e in journal),
                               done=sum(d['report']['outcome'] == 'done' for d in result.trace['decision_trace']),
                               sha256=hashlib.sha256(result.trace_bytes).hexdigest(), trace=str(result.path / 'trace.json'))
                    if condition == 'full' and row['done'] == 10 and not problems and (mode == 'live' or letter == 'C'):
                        candidates.append(result)
                rows.append(row)
                write_csv(output / 'matrix.csv', rows, list(row))
    mapping = life_packet(candidates, output / 'blind', output / 'private/key.json')
    summary = {'mechanism': 'life-v1', 'mode': mode, 'cells': len(rows),
               'executed': sum(r['status'] == 'executed' for r in rows), 'episodes': len(mapping),
               'runs': rows, 'human_ratings': None,
               'limitations': 'Конечный симулируемый мир, фиксированные входящие, временная биография. '
                              'Субъективное переживание и полное сходство с человеком не проверены. '
                              'Новые адаптеры A/B/C не заменяют исходную матрицу S03.'}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary
