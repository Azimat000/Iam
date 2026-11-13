"""Авторство и соседство доступных реплик; журнал даёт только границы и порядок."""


def authorship(turn):
    seq, row, direction, person, text, outcome, received_seq = turn
    return {'speaker': 'interlocutor' if direction == 'incoming' else 'self',
            'person': person, 'direction': direction, 'seq': seq, 'received_seq': received_seq,
            'outcome': outcome, 'statement_scope': 'utterance_only',
            'evidence_rule': 'Контакт не доказывает автора цитаты; прежнее сомнение не отменяет показанный источник.'}


def boundaries(memory):
    actions = memory.ctl.state['world_time'].get('world', {}).get('actions', {})
    return {event.seq for event in memory.ctl.events_after(0)
                  if event.type in ('input.arrived', 'input.read', 'message.out_sent', 'message.out_unknown')
                  or event.type == 'action.failed'
                  and actions.get(event.payload.get('action_id'), {}).get('kind') == 'message'}


def adjacent(memory, turns):
    edges = boundaries(memory)
    result = {}
    for i, turn in enumerate(turns):
        j = i + (1 if turn[2] == 'incoming' else -1)
        if not 0 <= j < len(turns):
            continue
        other = turns[j]
        low, high = sorted((turn[0], other[0]))
        if (turn[3] == other[3] and turn[2] != other[2] and other[4] is not None
                and not any(low < seq < high for seq in edges)):
            result[turn[1].memory_id] = other
    return result


def neighbor_context(memory, trigger, records, context):
    if memory.off or getattr(trigger, 'type', None) != 'input.read' or not records:
        return []
    turns = memory._turns()
    links = adjacent(memory, turns)
    shown = {item['ref'] for item in context}
    # Обычные результаты имеют приоритет; исторические окна тоже могут дать связь.
    seeds = list(dict.fromkeys([row.memory_id for row in records] + [item['ref'] for item in context]))
    result, remaining = [], 2000
    for ref in seeds:
        turn = links.get(ref)
        if turn is None:
            continue
        row = turn[1]
        if row.memory_id in shown or trigger.event_id in row.sources:
            continue
        packed = memory._window(row, recall_role='neighbor', limit=min(remaining, memory.text_limit),
                                metadata={'neighbor_of': ref, 'neighbor_relation': 'adjacent_not_verified_reply'})
        if packed is None:
            continue
        item = {'ref': row.memory_id, 'origin': row.origin, 'layer': row.layer, 'text': packed}
        if memory._safe_item(item):
            result.append(item)
            remaining -= len(packed)
            shown.add(row.memory_id)
        if len(result) == 2:
            break
    return result
