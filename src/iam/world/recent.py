"""Последняя доступная работа и результаты только из уже показанных окон памяти."""
import json

from . import WRITE


def result_view(memory, row, context, state):
    if row.origin != 'observation' or row.version != 1 or row.content.get('outcome') != 'confirmed':
        return None
    action = state.get('actions', {}).get(row.content.get('action_id'), {})
    receipt = action.get('receipt')
    if not receipt or action.get('status') != 'confirmed' or receipt['event_id'] not in row.sources:
        return None
    event = memory.ctl.find_event(event_id=receipt['event_id'])
    if event is None or event.type != 'action.confirmed':
        return None
    prefix = f"Подтверждён успех: {action['kind']} {action['capability_id']}. "
    if not row.text.startswith(prefix):
        return None
    for item in context:
        if item['ref'] != row.memory_id or item['layer'] != row.layer:
            continue
        try:
            packet = json.loads(item['text'])
        except (ValueError, TypeError):
            continue
        if not isinstance(packet, dict) or packet.get('version') != row.version:
            continue
        window = packet.get('text_window', {})
        if not isinstance(window, dict):
            continue
        start, end = window.get('start'), window.get('end')
        if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(row.text)
                or packet.get('text') != row.text[start:end] or end <= len(prefix)):
            continue
        start = max(start, len(prefix))
        public = {key: value for key, value in receipt.items() if key != 'previous_fingerprint'}
        return {'memory_id': row.memory_id, **public,
                'action_id': row.content['action_id'], 'project_id': receipt['target'],
                'seq': event.seq, 'version': row.version,
                'current_work': receipt['scope'] == 'artifact'
                    and state.get('projects', {}).get(receipt['target'], {}).get('latest_work') == row.content['action_id'],
                'observation_scope': memory.observation_scope(row),
                'text': row.text[start:end],
                'result_window': {'start': start, 'end': end, 'total': len(row.text), 'more': end < len(row.text)},
                'result_complete': start == len(prefix) and end == len(row.text)}
    return None


def recent_work(memory, state, context):
    if memory.off:
        return [], None
    # Порядок берём из подтверждающих событий, никогда из текста работ/журнала.
    candidates = []
    rows = {row.memory_id: row for row in memory.records()}
    for aid, action in state.get('actions', {}).items():
        receipt = action.get('receipt', {})
        project = receipt.get('target')
        latest = state.get('projects', {}).get(project, {}).get('latest_work')
        if (action.get('capability_id') != WRITE or action.get('status') != 'confirmed'
                or receipt.get('scope') != 'artifact' or latest != aid):
            continue
        event = memory.ctl.find_event(event_id=receipt['event_id'])
        if event is not None and event.type == 'action.confirmed':
            candidates.append((event.seq, aid, project, receipt['event_id']))
    if not candidates:
        return [], None
    seq, aid, project, event_id = max(candidates)
    row = next((row for row in rows.values() if row.origin == 'observation' and row.version == 1
                and row.content.get('outcome') == 'confirmed' and row.content.get('action_id') == aid
                and event_id in row.sources), None)
    # Если последняя версия недоступна, прошлую не выдаём за последнюю.
    if row is None:
        return [], None
    shown = result_view(memory, row, context, state)
    if shown is not None:
        # Уже показанный фрагмент не режем повторно ради дублирующего нового окна.
        # В каталог он поступал и раньше; его тело теперь ограничено этим окном.
        shown.update(current_work=True, project_id=project, seq=seq, version=row.version)
        return ([], shown) if memory._safe_item(shown) else ([], None)
    metadata = {'recall_role': 'recent_activity', 'action_id': row.content['action_id'],
                'project_id': project, 'seq': seq, 'current_work': True,
                'meaning': 'последняя доступная актуальная версия собственной работы; не оценка её качества'}
    # Показанное окно и его дублируемый фрагмент в каталоге вместе укладываются в2000.
    low, high, selected = 0, min(2000, memory.text_limit), None
    while low <= high:
        size = (low + high) // 2
        packed = memory._window(row, recall_role='recent_activity', metadata=metadata, limit=size)
        if packed is None:
            low = size + 1
            continue
        item = {'ref': row.memory_id, 'origin': row.origin, 'layer': row.layer, 'text': packed}
        result = result_view(memory, row, [item], state)
        if result is None:
            low = size + 1
            continue
        if len(packed) + len(json.dumps(result, ensure_ascii=False)) <= 2000:
            selected = item, result
            low = size + 1
        else:
            high = size - 1
    if selected is None or not memory._safe_item(selected[0]) or not memory._safe_item(selected[1]):
        return [], None
    return [selected[0]], selected[1]
