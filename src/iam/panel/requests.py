"""Просьба, ответ и сообщённый результат в панели."""

from html import escape

from iam.creator_bridge import records


def render_requests(data, token, form, event_rows):
    requests = records(data["state"])
    if not requests:
        return '<p class="empty">Просьб пока нет.</p>'
    sections = ['<p>Ответ здесь сохраняется в истории просьбы. Доступ к средствам меняется на странице «Возможности».</p>']
    outcomes = {"declined": "Отказано", "waiting": "Ожидается результат",
                "operator_reported": "Создатель сообщил результат"}
    for number, (key, item) in enumerate(reversed(list(requests.items())[-50:]), 1):
        status = item["status"]
        crq_id = item.get("crq_id")
        controls = []
        if status in ("accepted", "open"):
            controls.extend((("accept", "Согласиться"), ("decline", "Отказать")))
        if item.get("outcome") == "waiting" and status == "answered":
            controls.append(("record_result", "Записать результат"))
        if status in ("open", "answered"):
            controls.append(("circumstances", "Отметить изменение обстоятельств"))
        section = [f'<section class="creator-request"><h2>Просьба {number}</h2><p>{escape(item["text"])}</p>']
        if status == "accepted":
            section.append('<p>Просьба сохранена. Отправка не началась.</p>')
        if item.get("outcome"):
            section.append(f'<p>{outcomes[item["outcome"]]}</p>')
        for field, label in (("answer", "Ответ"), ("result", "Результат"), ("circumstances", "Новые обстоятельства")):
            if item.get(field):
                section.append(f'<p><strong>{label}</strong> {escape(item[field])}</p>')
        for reply in data["telegram_replies"].get(crq_id, []):
            section.append(f'<p><strong>Прочитанный ответ в Telegram</strong> {escape(reply["text"])}</p>')
        if status == "retry_allowed":
            section.append('<p>Можно снова обратиться с этой просьбой один раз.</p>')
        if crq_id and controls:
            body = (f'<input type="hidden" name="request_key" value="{escape(key, quote=True)}">'
                    f'<input type="hidden" name="crq_id" value="{escape(crq_id, quote=True)}">')
            section.append(form("creator_response", "", token, data, body=body, reason="",
                                form_id=crq_id, choices=controls, reason_label="Комментарий к решению"))
        sections.append("".join(section) + "</section>")
    sections.append('<section><h2>История обращений</h2>' + event_rows(data["requests"]) + '</section>')
    return "".join(sections)
