"""Лексические кандидаты предмета: первичные видимые слова, не вывод о намерении."""
import json

from iam.memory.search import words, match
from . import WRITE

# Слова о ходе беседы/редактировании не различают предметы работ.
GENERIC = words('сцена сцены сцене работу работа работы работе текст текста тексте вариант варианте версии версия последней последний прежней прежний новой новый деталь детали нравится думаешь продолжим спасибо сейчас осталось оставь отдельно какой какая какое какие какую каким каких сам сама само сами самой самому самых себе себя тебе тебя твой твоя твои свой своя свои этой этот этом этих ту те здесь тогда только можно можешь почему когда где чтобы потом теперь снова пусть ведь даже просто очень более меня него нее нам вам им наш ваш')


def project_source(memory, state, row):
    if row.origin != 'observation' or row.version != 1 or row.content.get('outcome') != 'confirmed':
        return None
    action = state.get('actions', {}).get(row.content.get('action_id'), {})
    receipt = action.get('receipt', {})
    if (action.get('capability_id') != WRITE or action.get('status') != 'confirmed'
            or receipt.get('scope') != 'artifact' or receipt.get('event_id') not in row.sources):
        return None
    event = memory.ctl.find_event(event_id=receipt['event_id'])
    return receipt['target'] if event is not None and event.type == 'action.confirmed' else None


def subject_candidates(memory, state, context, turns, current, rows):
    """None — нет лексического основания; список — кандидаты с первичными ссылками."""
    documents = {}
    for row in rows.values():
        project = project_source(memory, state, row)
        if project is not None:
            # Технический префикс подтверждения не является предметом работы.
            body = row.text.partition('. ')[2]
            documents.setdefault(project, set()).update(words(body) - GENERIC)
    primary = {}
    incoming = {t[1].memory_id: t for t in memory._turns() if t[2] == 'incoming'}
    for item in context:
        try:
            packet = json.loads(item['text'])
        except (ValueError, TypeError):
            continue
        row = rows.get(item['ref'])
        source = incoming.get(item['ref'])
        if (not isinstance(packet, dict) or row is None or row.version != 1 or source is None
                or packet.get('person') != source[3] or packet.get('seq') != source[0]
                or packet.get('person') != current.get('person')
                or packet.get('direction') != 'incoming' or packet.get('outcome') != 'read'):
            continue
        # Только точный уже предъявленный фрагмент доступной записи.
        window = packet.get('excerpt', packet.get('text_window', {}))
        if not isinstance(window, dict):
            continue
        start, end = window.get('start'), window.get('end')
        if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(row.text)
                or packet.get('text') != row.text[start:end]):
            continue
        primary[item['ref']] = packet
    current_ref = next((item['ref'] for item, turn in turns if turn is current), None)

    def candidates(items):
        found, shared = {}, []
        for ref, packet in items:
            terms = {w for w in words(packet['text']) - GENERIC if len(w) >= 4}
            for term in terms:
                projects = {project for project, tokens in documents.items()
                            if any(match(term, token) for token in tokens)}
                # Общая для нескольких работ лексика сама по себе не различает тему.
                if len(projects) == 1:
                    found.setdefault(next(iter(projects)), ref)
                else:
                    shared.append((projects, ref))
        unique = set(found)
        for projects, ref in shared:
            if not projects & unique:
                for project in projects:
                    found.setdefault(project, ref)
        return found

    explicit = candidates([(current_ref, primary[current_ref])]) if current_ref in primary else {}
    if explicit:
        return explicit
    # Нельзя автоматически выбрать последнюю собственную ошибочную реплику.
    # Несовместимые первичные темы в видимом окне остаются неоднозначностью.
    visible_turns = {item['ref'] for item, _ in turns}
    previous = candidates([(ref, packet) for ref, packet in primary.items()
                           if ref != current_ref and ref in visible_turns])
    return previous or None
