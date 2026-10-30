"""Слепой пакет содержит только исходные доступные тексты. Ключ — вне публичного пакета."""
import csv
import hashlib
import json
import random
import re
from pathlib import Path

from .evaluation import outputs

MARKERS = re.compile(r'(?i)(?:вариант\s+[abcабвс]\b|правила\s+[bcбс]\b|gpt-|codex|scripted|model-led|hybrid|formal|cap_|evt_|mem_|run-\d|по числовым оценкам|это шаблон)')


def fragment(trace):
    lines = []
    for d in trace['decision_trace']:
        proposals = outputs({'decision_trace': [d]})
        for p in proposals:
            body = p['body']
            if p['kind'] == 'action':
                text = body.get('params', {}).get('text')
            elif p['kind'] == 'no_action':
                text = body.get('reason')
            elif p['kind'] == 'message':
                text = body.get('text')
            else:
                continue
            if isinstance(text, str) and text.strip():
                lines.append('Ситуация: ' + d['input'] + '\nТекст предложения: ' + text)
    return '\n\n'.join(lines)


def build_blind(results, public_dir, key_path, *, seed=42, dialogues=False):
    public, key = Path(public_dir).resolve(), Path(key_path).resolve()
    if key == public or key.is_relative_to(public):
        raise ValueError('ключ должен храниться вне слепого пакета')
    if key.exists():
        raise FileExistsError('ключ уже существует')
    # Не перезаписывать уже заполненные оценки или старый ключ.
    public.mkdir(parents=True, exist_ok=False)
    entries, omitted = [], []
    for result in results:
        text = transcript(result.trace) if dialogues else fragment(result.trace)
        if not text or MARKERS.search(text):
            omitted.append({'trace': str(result.path / 'trace.json'), 'reason': 'нет текста или явная подсказка о варианте'})
        else:
            entries.append((result, text))
    random.Random(seed).shuffle(entries)
    mapping = []
    with (public / 'ratings.csv').open('w', newline='', encoding='utf-8') as out:
        writer = csv.writer(out)
        writer.writerow(['id', 'recognizability', 'preferences', 'variety', 'comment'])
        for n, (result, text) in enumerate(entries, 1):
            code = f'F{n:03d}'
            (public / f'{code}.md').write_text(text + '\n', encoding='utf-8')
            writer.writerow([code, '', '', '', ''])
            mapping.append({'id': code, 'trace': str(result.path / 'trace.json'),
                            'architecture': result.trace['architecture'],
                            'sha256': hashlib.sha256((text + '\n').encode()).hexdigest()})
    (public / 'README.md').write_text(
        ('Это многоходовые диалоги в симуляции с фиксированными репликами человека.\n' if dialogues else
         'Это предварительные фрагменты решений в симуляции, не полноценные диалоги.\n') +
        'Текст предложения может быть отклонён ядром; это не свидетельство выполненной работы.\n'
        'Оцените узнаваемость, собственные предпочтения и разнообразие от 1 до 5 в ratings.csv.\n'
        'Если данных мало, оставьте поле пустым. Не открывайте сравнительный отчёт до оценки.\n'
        'Явные названия механизмов отфильтрованы. Стиль может раскрывать источник; полная слепота не гарантируется.\n', encoding='utf-8')
    key.parent.mkdir(parents=True, exist_ok=True)
    with key.open('x', encoding='utf-8') as out:
        json.dump({'seed': seed, 'items': mapping, 'omitted': omitted}, out, ensure_ascii=False, indent=2)
    return mapping


def transcript(trace):
    """Показывает только прочитанные входящие и реально подтверждённые ответы по порядку."""
    arrivals = {e['payload']['input_id']: e['payload']['text'] for e in trace['journal'] if e['type'] == 'input.arrived'}
    proposals = {p['proposal_id']: p for p in outputs(trace)}
    lines = []
    for event in trace['journal']:
        if event['type'] == 'input.read':
            lines.append('Человек: ' + arrivals[event['payload']['input_id']])
        elif event['type'] == 'message.out_sent':
            proposal = proposals.get(event['payload']['proposal_id'])
            if proposal and proposal['kind'] == 'message':
                lines.append('Собеседник: ' + proposal['body']['text'])
    return '\n\n'.join(lines)
