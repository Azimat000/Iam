"""Обратимое представление уже разрешённого контекста без нового извлечения.

Ядро по-прежнему хранит исходные блоки. Здесь меняется только их предъявление:
текущий разговор расположен перед состоянием, а машинный JSON раскрыт один раз.
Тексты, окна и источники не объединяются и не пересказываются.
"""
from copy import deepcopy
import json


CONVERSATION_PARTS = ("current", "recent_turns", "recalled_utterances", "evidence", "unread")
OTHER_PARTS = ("experience", "state_and_choices", "other_context")
STATE_KINDS = {"character", "proposal_feedback", "social_update_option", "social_readiness",
               "discussion_project"}


def _decode(item):
    """Раскрываем только известный конверт с точно восстановимым JSON-кодированием."""
    value = item.get("text")
    try:
        data = json.loads(value)
        if not isinstance(data, dict) or json.dumps(data, ensure_ascii=False) != value:
            return None
    except (ValueError, TypeError, RecursionError):
        return None
    layer, kind = item.get("layer"), data.get("kind")
    if layer == "state":
        signatures = (
            kind == "current_interaction" and {"read_now", "unread_targets"} <= data.keys(),
            kind == "dialogue_turn" and {"speaker", "person", "seq", "text", "excerpt"} <= data.keys(),
            kind == "evidence_review" and "recollection_sources" in data,
            kind in STATE_KINDS if isinstance(kind, str) else False,
            {"library", "projects", "intentions", "world_results"} <= data.keys(),
            {"choices", "current_focus"} <= data.keys(),
            {"person", "history", "closeness", "trust_as_source"} <= data.keys(),
        )
        return data if any(signatures) else None
    if layer in ("personal", "interpretation"):
        memory = {"version", "sources", "text_window"} <= data.keys()
        interpretation = "sources" in data and any(key in data for key in (
            "emotion", "skill_assessment", "procedure", "belief", "text"))
        return data if memory or interpretation else None
    if layer == "notice" and {"notice_level", "count", "input_id"} <= data.keys():
        return data
    return None


def _place(item, data):
    if data is not None:
        kind = data.get("kind")
        if item["layer"] == "notice":
            return "unread"
        if kind == "current_interaction":
            return "current"
        if kind == "dialogue_turn":
            return "recent_turns"
        if kind == "evidence_review":
            return "evidence"
        if (item["layer"] == "personal" and data.get("statement_scope") == "utterance_only"
                and data.get("speaker") in ("self", "interlocutor")):
            return "recalled_utterances"
        return "experience" if item["layer"] in ("personal", "interpretation") else "state_and_choices"
    # Обычные инструкции не разбираются как речь или содержимое памяти.
    if item.get("layer") == "state" and not item.get("text", "").lstrip().startswith(("{", "[")):
        return "state_and_choices"
    return "other_context"


def present_context(context):
    """Возвращает независимое представление; никогда не обращается к памяти/архиву.

    ``index`` хранит исходный порядок, ``text`` известного машинного блока становится
    объектом. Неизвестные блоки сохраняют буквальный text. Все остальные поля остаются
    в ``item``, поэтому расширения исходного конверта также не теряются.
    """
    conversation = {key: [] for key in CONVERSATION_PARTS}
    view = {"conversation": conversation, **{key: [] for key in OTHER_PARTS}}
    for index, original in enumerate(context):
        item = deepcopy(original)
        data = _decode(item)
        destination = _place(item, data)
        if data is not None:
            item["text"] = data
        entry = {"index": index, "item": item}
        (conversation[destination] if destination in conversation else view[destination]).append(entry)
    for name in ("recent_turns", "recalled_utterances"):
        # Порядок чтения/исхода не означает связь вопрос–ответ; метаданные остаются.
        conversation[name].sort(key=lambda entry: (
            type(entry["item"]["text"].get("seq")) is not int,
            entry["item"]["text"].get("seq") if type(entry["item"]["text"].get("seq")) is int else 0,
            entry["index"]))
    return view


def restore_context(view):
    """Точное обратное преобразование для проверки входа модели и совместимости."""
    entries = [entry for name in CONVERSATION_PARTS for entry in view["conversation"][name]]
    entries += [entry for name in OTHER_PARTS for entry in view[name]]
    result = []
    for entry in sorted(entries, key=lambda value: value["index"]):
        item = deepcopy(entry["item"])
        if isinstance(item["text"], dict):
            item["text"] = json.dumps(item["text"], ensure_ascii=False)
        result.append(item)
    return result
