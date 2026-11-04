"""Страница наблюдения за представлением личности о себе."""
from iam.selfmodel import LEVELS, PARTS

FIELDS = {
    "emotion": "Эмоции", "load": "Нагрузка", "attention": "Внимание", "traits": "Черты",
    "values": "Ценности", "likes": "Предпочтения", "capabilities": "Возможности", "methods": "Освоенные приёмы",
    "reaction": "Реакция", "mood": "Настроение", "tendency": "Склонность", "focus": "Фокус",
    "closeness": "Близость", "trust_as_source": "Доверие к сведениям",
}
STATUSES = {
    "available": "доступно", "unavailable_temporarily": "временно недоступно",
    "requires_permission": "требуется разрешение", "forbidden": "запрещено",
}


def _list(items, escape):
    lines = "".join(f"<li>{escape(item)}</li>" for item in items)
    return f"<ul>{lines}</ul>" if lines else ""


def _records(items, escape):
    if not items:
        return ""
    return "<ul>" + "".join(f'<li>{escape(item["text"])} <small>· {escape(item["ref"])}</small></li>'
                             for item in items) + "</ul>"


def _parts(part, value, escape):
    if not value:
        return ""
    if part in ("history", "fallibility"):
        return _records(value, escape)
    if part == "relations_view":
        return "".join(
            f'<p>{escape("Создатель" if key == "creator" else key)} · записей об общении: {len(person["history"])}</p>'
            + _list((f'{FIELDS[field]} — {person[field]}' for field in ("closeness", "trust_as_source")
                     if person[field] is not None), escape)
            + _list(person["history"], escape)
            for key, person in value.items()
        )
    if part == "skills_view":
        return _list((f'{cap["name"]} · {STATUSES.get(cap["status"], cap["status"])}'
                      for cap in value["capabilities"]), escape) + _records(value["methods"], escape)
    content = []
    for key in ("traits", "values", "likes"):
        if value.get(key):
            content.append(f'<p>{FIELDS[key]}</p>' + _list(
                (f'{item.get("name", item.get("topic_or_activity", ""))} — '
                 f'{item.get("value", item.get("weight", item.get("score", "")))}'
                 for item in value[key] if isinstance(item, dict)), escape))
    for key in ("emotion", "load", "attention"):
        if key in value:
            raw = value[key]
            if isinstance(raw, dict):
                shown = [f"{FIELDS[k]} — {raw[k]}" for k in ("reaction", "mood", "tendency", "focus") if k in raw]
                content.append(f'<p>{FIELDS[key]}</p>' + _list(shown, escape))
            else:
                content.append(f'<p>{FIELDS[key]} — {escape(raw)}</p>')
    refs = value.get("refs", [])
    if refs:
        content.append('<details><summary>Источники</summary>' + _list(refs, escape) + '</details>')
    return "".join(content)


def render_selfmodel(view, escape):
    content = ['<p>Представление о себе складывается из состояния и доступного опыта. '
               'Размышления ниже помечены как объяснения модели.</p><section><h2>Шесть частей</h2>']
    for part, label in PARTS.items():
        body = _parts(part, view["parts"][part], escape)
        content.append(f'<details><summary>{label}</summary>')
        content.append(body or '<p class="empty">Доступных сведений пока нет.</p>')
        content.append('</details>')
    content.append('</section><section><h2>История размышлений</h2>')
    if not view["history"]:
        content.append('<p class="empty">Пока нет размышлений с доступными основаниями.</p>')
    for entry in view["history"][:50]:
        label = LEVELS[entry["level"]]
        part = PARTS[entry["part"]]
        content.append(f'<article><h3>{label} · {part}</h3><p class="memory-text">{escape(entry["text"])}</p>'
                       f'<p class="help">Объяснение модели · версия {entry["state_version"]}</p>')
        if entry["level"] == "stable_change":
            content.append(f'<p>{escape(entry["trait"])} · {escape(entry["before"])} → {escape(entry["after"])}</p>')
        refs = (*entry["refs"], *entry["traces"])
        content.append('<details><summary>Основания</summary>' + _list(refs, escape) + '</details></article>')
    return "".join(content) + '</section>'
