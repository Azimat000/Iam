"""Связь видимой доставленной реплики с доступной работой, без чтения архивных тел."""
import json

from .subject import project_source, subject_candidates
from .recent import recent_work


def delivered_proposal(memory, event):
    """Только ответ, причинно связанный с этой подтверждённой отправкой."""
    outgoing = event
    for kind in ('message.out_requested', 'proposal.accepted', 'proposal.received', 'proposal.requested'):
        parent = next((e for ref in event.causes if (e := memory.ctl.find_event(event_id=ref)) is not None
                       and e.type == kind and e.seq < event.seq), None)
        if parent is None:
            return None
        if kind == 'proposal.accepted':
            accepted = parent
        elif kind == 'proposal.received':
            response = parent.payload['response']
        event = parent
    request = event.payload['request']
    if (accepted.payload.get('proposal_id') != outgoing.payload.get('proposal_id')
            or accepted.payload.get('request_id') != request['request_id']
            or outgoing.payload.get('cycle_id') != request['cycle_id']):
        return None
    return next((p for p in response['proposals'] if p['proposal_id'] == outgoing.payload['proposal_id']
                 and p['kind'] == 'message'), None)


def discussion_work(memory, state, context):
    if memory.off:
        return [], None
    turns = []
    for item in context:
        try:
            packet = json.loads(item['text'])
        except (ValueError, TypeError):
            continue
        if isinstance(packet, dict) and packet.get('kind') == 'dialogue_turn':
            turns.append((item, packet))
    current = next((t for _, t in turns if t.get('current_read')), None)
    if current is None:
        return [], None
    rows = {r.memory_id: r for r in memory.records()}
    candidates = subject_candidates(memory, state, context, turns, current, rows)
    if candidates is not None:
        if not candidates:
            item = next(item for item, turn in turns if turn is current)
            packet = {'kind': 'discussion_project', 'unresolved': True, 'message_ref': item['ref'],
                      'available_current_result': False,
                      'meaning': 'Явный предмет текущей реплики не сопоставлен доступной работе. '
                                 'Уточни предмет; прежний собственный ответ не подтверждает другую тему.'}
            if memory._safe_item(packet):
                return [{'ref': item['ref'], 'origin': 'observation', 'layer': 'state',
                         'text': json.dumps(packet, ensure_ascii=False)}], None
            return [], None
        if len(candidates) == 1:
            project, ref = next(iter(candidates.items()))
            extra, result = recent_work(memory, state, context, project_id=project, discussion_ref=ref)
            if result is not None:
                return extra, result
            packet = {'kind': 'discussion_project', 'message_ref': ref, 'project_id': project,
                      'available_current_result': False,
                      'meaning': 'Первичная реплика указывает на кандидата проекта; актуальный текст недоступен.'}
        else:
            packet = {'kind': 'discussion_project', 'ambiguous': True,
                      'candidates': [{'project_id': project, 'message_ref': ref}
                                     for project, ref in sorted(candidates.items())][:8],
                      'meaning': 'Первичные слова совместимы с несколькими проектами. Уточни предмет, '
                                 'не подставляй последнее занятие или собственный прошлый ответ.'}
        if len(json.dumps(packet, ensure_ascii=False)) <= 2000 and memory._safe_item(packet):
            return [{'ref': next(iter(candidates.values())), 'origin': 'observation', 'layer': 'state',
                     'text': json.dumps(packet, ensure_ascii=False)}], None
        return [], None
    # Журнал даёт лишь подтверждение доставки и ссылки; слова берём только
    # из доступных уже показанных сообщений, текст работы — через recent_work.
    for item, turn in sorted(turns, key=lambda pair: pair[1]['seq'], reverse=True):
        row = rows.get(item['ref'])
        if (turn.get('direction') != 'outgoing' or turn.get('outcome') != 'confirmed'
                or turn.get('person') != current.get('person') or not turn.get('content_available')
                or row is None or row.version != turn.get('version') or len(row.sources) != 1):
            continue
        event = memory.ctl.find_event(event_id=row.sources[0])
        if event is None or event.type != 'message.out_sent':
            continue
        proposal = delivered_proposal(memory, event) or {}
        projects = set()
        for ref in proposal.get('cites', []):
            source = rows.get(ref)
            project = project_source(memory, state, source) if source is not None else None
            if project is not None:
                projects.add(project)
        if len(projects) != 1:
            continue
        project = next(iter(projects))
        extra, result = recent_work(memory, state, context, project_id=project, discussion_ref=item['ref'])
        if result is not None:
            return extra, result
        # Не выдаём последнюю работу другого проекта за этот недоступный текст.
        packet = {'kind': 'discussion_project', 'message_ref': item['ref'], 'project_id': project,
                  'available_current_result': False,
                  'meaning': 'Этот проект упомянут в основаниях видимой реплики; актуальный текст недоступен. '
                             'Это кандидат предмета разговора, не доказанное намерение собеседника.'}
        if memory._safe_item(packet):
            return [{'ref': item['ref'], 'origin': 'observation', 'layer': 'state',
                     'text': json.dumps(packet, ensure_ascii=False)}], None
    return [], None
