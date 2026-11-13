"""Последняя работа и ограниченные результаты из доступной памяти."""
import json

from . import WRITE


def result_view(memory, row, context, state, *, full_catalog=False):
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
        # Прежний каталог — самостоятельное предъявление полного результата
        # уже видимой записи (до8000). Новые добавочные окна не расширяем.
        catalog_window = full_catalog and packet.get('recall_role') in (None, 'historical_episode')
        if catalog_window:
            start, end = len(prefix), min(len(row.text), len(prefix) + 8000)
        else:
            start = max(start, len(prefix))
        public = {key: value for key, value in receipt.items() if key != 'previous_fingerprint'}
        return {'memory_id': row.memory_id, **public,
                'action_id': row.content['action_id'], 'project_id': receipt['target'],
                'seq': event.seq, 'version': row.version,
                'current_work': receipt['scope'] == 'artifact'
                    and state.get('projects', {}).get(receipt['target'], {}).get('latest_work') == row.content['action_id'],
                'observation_scope': memory.observation_scope(row),
                'text': row.text[start:end],
                'result_presentation': 'catalog_window' if catalog_window else 'memory_fragment',
                'result_window': {'start': start, 'end': end, 'total': len(row.text), 'more': end < len(row.text)},
                'result_complete': start == len(prefix) and end == len(row.text)}
    return None


def recent_work(memory, state, context, *, project_id=None, discussion_ref=None):
    if memory.off:
        return [], None
    # Порядок берём из подтверждающих событий, никогда из текста работ/журнала.
    candidates = []
    rows = {row.memory_id: row for row in memory.records()}
    for aid, action in state.get('actions', {}).items():
        receipt = action.get('receipt', {})
        project = receipt.get('target')
        latest = state.get('projects', {}).get(project, {}).get('latest_work')
        if (project_id is not None and project != project_id
                or action.get('capability_id') != WRITE or action.get('status') != 'confirmed'
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
    shown = result_view(memory, row, context, state, full_catalog=True)
    if shown is not None:
        # Уже видимая запись сохраняет прежний полный канал каталога.
        # Тело берём из памяти; дополнительное окно для неё не создаём.
        shown.update(current_work=True, project_id=project, seq=seq, version=row.version)
        if discussion_ref:
            shown['discussion_ref'] = discussion_ref
        return ([], shown) if memory._safe_item(shown) else ([], None)
    # Содержание один раз — в каталоге; отдельный ref сохраняет право цитирования.
    # Длинная работа автоматически представляется начальным фрагментом, не целым.
    prefix = f"Подтверждён успех: {state['actions'][aid]['kind']} {WRITE}. "
    if not row.text.startswith(prefix):
        return [], None
    index = {'recall_role': 'recent_activity', 'version': row.version,
             'current_work': True, 'project_id': project, 'content_in': 'world_results',
             'sources': list(row.sources), 'observation_scope': memory.observation_scope(row)}
    if discussion_ref:
        index['discussion_ref'] = discussion_ref
    low, high, selected = len(prefix) + 1, len(row.text), None
    while low <= high:
        end = (low + high) // 2
        # result_view проверяет точное соответствие доступной записи и подтверждению.
        window = json.dumps({'version': row.version, 'text': row.text[len(prefix):end],
                             'text_window': {'start': len(prefix), 'end': end}}, ensure_ascii=False)
        result = result_view(memory, row, [{'ref': row.memory_id, 'layer': row.layer, 'text': window}], state)
        if result is None:
            return [], None
        result['result_presentation'] = 'automatic_recollection'
        if discussion_ref:
            result['discussion_ref'] = discussion_ref
        index['text_window'] = result['result_window']
        packed_index = json.dumps(index, ensure_ascii=False)
        if len(packed_index) + len(json.dumps(result, ensure_ascii=False)) <= min(2000, memory.text_limit):
            selected = packed_index, result
            low = end + 1
        else:
            high = end - 1
    if selected is None:
        return [], None
    packed_index, result = selected
    item = {'ref': row.memory_id, 'origin': row.origin, 'layer': row.layer, 'text': packed_index}
    if not memory._safe_item(item) or not memory._safe_item(result):
        return [], None
    return [item], result
