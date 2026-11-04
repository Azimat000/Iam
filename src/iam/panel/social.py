"""Текущие отношения, отдельно от убеждений и готовности к изменениям."""

from html import escape


def render_social(overview):
    people = ['<section><h2>Отношения и общение</h2>']
    for key, person in overview["people"].items():
        title = "Создатель" if key == "creator" else key
        people.append(f'<article><h3>{escape(title)}</h3>'
                      f'<p>Доступных записей об общении: {len(person["history"])}.</p>')
        if person.get("last_contact_calendar"):
            people.append(f'<p>Последнее общение: {escape(person["last_contact_calendar"])}.</p>')
        if person["history"]:
            refs = ", ".join(person["history"][-3:])
            people.append(f'<p class="help">Последние записи: {escape(refs)}</p>')
        for key, label in (("closeness", "Близость"), ("trust_as_source", "Доверие к сведениям")):
            value = person[key]
            people.append(f'<p>{label} — {escape(str(value)) if value is not None else "пока без оценки"}.</p>')
        people.append("</article>")
    for key, label in (("agreement", "Согласие с утверждениями"), ("readiness", "Готовность изменить поведение")):
        items = overview[key]
        people.append(f"<h3>{label}</h3>")
        if items:
            people.append("<ul>" + "".join(f'<li>{escape(name)} — {escape(str(value))}</li>'
                                          for name, value in items.items()) + "</ul>")
        else:
            people.append("<p>Пока без оценок.</p>")
    return "".join(people) + "</section>"
