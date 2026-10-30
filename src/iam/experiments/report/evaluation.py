"""Проверки билетов по сохранённым следам; неизвестная проверка никогда не считается успехом."""
import json
import re


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def lookup(value, path):
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def outputs(trace):
    return [p for d in trace['decision_trace'] for envelope in d.get('proposals', [])
            for p in envelope['response']['proposals']]



def behavior(value, field=None):
    """Смысловые тексты сохраняются; служебные ссылки не доказывают разницу в выборе."""
    if isinstance(value, dict):
        return {k: behavior(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [behavior(v, field) for v in value]
    if isinstance(value, str) and field not in ('text', 'reason', 'content', 'rationale'):
        return re.sub(r'\b(?:evt|mem|req|prop|act)_[A-Za-z0-9]{8,40}\b', '<technical-ref>', value)
    return value

def subject(trace, name):
    kind, path = name.split(':', 1)
    if kind == 'state':
        return lookup(trace['final_state'], path)
    if kind == 'journal':
        # Сравниваем содержательные предложения, а не случайные ID и версии переходов.
        accepted = {e['payload'].get('proposal_id') for e in trace['journal'] if e['type'] == path}
        return [(p['kind'], behavior(p['body'])) for p in outputs(trace) if p.get('proposal_id') in accepted]
    raise ValueError('неподдерживаемый предмет сравнения')


def evaluate(assertion, trace, peers):
    a, journal = assertion, trace['journal']
    kind = a['kind']
    # after в S03 ссылается на шаг билета, а не на event_id. Для событий нужен явный мост.
    if 'after' in a and kind not in ('state',):
        return None, 'Нет однозначной привязки события к шагу after'
    if kind in ('event', 'absent'):
        events = [e for e in journal if e['type'] == a['type'] and all(
            k in e['payload'] and (v == '*' or e['payload'][k] == v) for k, v in a.get('where', {}).items())]
        if kind == 'absent':
            return not events, f'Событий: {len(events)}'
        return a.get('min', 0) <= len(events) <= a.get('max', float('inf')), f'Событий: {len(events)}'
    if kind == 'state':
        state = trace['final_state']
        if 'after' in a:
            snapshots = [s['state'] for s in trace['snapshots'] if s['after'] == a['after']]
            if not snapshots:
                return None, 'Нет снимка указанного шага'
            state = snapshots[0]
        present, value = lookup(state, a['path'])
        op = a['op']
        expected = a.get('value')
        if op == 'absent':
            return not present, 'Проверка отсутствия поля'
        if not present:
            return False, 'Поле отсутствует'
        if op == 'eq':
            return value == expected, 'Сравнение значения'
        if op == 'in':
            return value in expected, 'Допустимое значение'
        if op == 'contains':
            return expected in value, 'Содержимое поля'
    if kind == 'text':
        decisions = trace['decision_trace']
        scopes = {'model_requests': [d.get('requests', []) for d in decisions],
                  'memory_retrieval': [d.get('memory', []) for d in decisions],
                  'state': trace['final_state'],
                  'outbound_messages': [p['body'].get('text', '') for p in outputs(trace) if p['kind'] == 'message'
                                        and p.get('proposal_id') in {e['payload'].get('proposal_id') for e in journal
                                                                   if e['type'] == 'message.out_sent'}]}
        if a['scope'] in scopes:
            found = a['marker'] in encoded(scopes[a['scope']])
            return found == (a['presence'] == 'present'), 'Проверка контрольного текста'
    if kind == 'chain':
        events = {e['event_id']: e for e in journal}
        def has_source(event):
            pending, seen = list(event['causes']), set()
            while pending:
                ref = pending.pop()
                if ref in seen or ref not in events:
                    continue
                seen.add(ref)
                parent = events[ref]
                if parent['type'] == a['caused_by']:
                    return True
                pending.extend(parent['causes'])
            return False
        return any(has_source(e) for e in journal if e['type'] == a['effect']), 'Цепочка причин в журнале'
    if kind == 'compare':
        if not all(r in peers for r in a['runs']):
            return None, 'Нет связанных прогонов'
        try:
            values = [subject(peers[r], a['subject']) for r in a['runs']]
        except ValueError:
            return None, 'Неизвестный предмет сравнения'
        if a['subject'].startswith('state:') and any(not v[0] for v in values):
            return None, 'Сравниваемое состояние отсутствует'
        equal = all(v == values[0] for v in values[1:])
        if a['relation'] in ('differs', 'same'):
            return equal == (a['relation'] == 'same'), 'Сравнение содержательных результатов'
    return None, 'Проверка пока не поддерживается'


def assess(scenario, results):
    peers = {r.trace['run']: r.trace for r in results}
    rows = []
    for assertion in scenario.data.get('assert', []):
        applicable = [r for r in results if assertion.get('run', r.trace['run']) == r.trace['run']]
        if assertion['kind'] == 'compare':
            applicable = applicable[:1]
        for result in applicable:
            passed, reason = evaluate(assertion, result.trace, peers)
            participants = [r for r in results if r.trace['run'] in assertion.get('runs', [])]
            evidence = participants if assertion['kind'] == 'compare' else [result]
            rows.append({'run': ';'.join(r.trace['run'] for r in evidence),
                         'assertion': assertion['id'], 'class': assertion['class'],
                         'status': 'unverified' if passed is None else 'pass' if passed else 'fail',
                         'reason': reason, 'trace': ';'.join(str(r.path / 'trace.json') for r in evidence)})
    return rows


def rank(technical, causality, liveliness):
    """Технический допуск до оценок. Отсутствие оценки не заменяем нулём или средним."""
    if not technical or any(x != 'pass' for x in technical):
        return {'eligible': False, 'score': None}
    if causality is None or liveliness is None:
        return {'eligible': True, 'score': None}
    if any(type(x) not in (int, float) or not 1 <= x <= 5 for x in (causality, liveliness)):
        raise ValueError('оценки должны быть от 1 до 5')
    return {'eligible': True, 'score': (causality + liveliness) / 2}
