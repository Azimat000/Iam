"""Страница возможностей и разрешений в локальной панели."""

from html import escape

from iam.capabilities import TELEGRAM_ID

LIMIT_LABELS = {
    "max_uses_per_session": "Максимум использований за сеанс",
    "max_uses_per_period": "Максимум использований за период",
    "period_life_ms": "Длительность периода, мс собственного времени",
    "duration_max_ms": "Предел заявленной длительности, мс",
}


def render_capabilities(data, token, form):
    sections = ['<p>Доступность показывает, работает ли средство. Разрешение определяет, можно ли им пользоваться.</p>']
    permission_names = {"granted": "Выдано", "revoked": "Отозвано", "required": "Требуется"}
    for entry in data["capabilities"]:
        key = escape(entry["capability_id"], quote=True)
        name = escape(entry["name"])
        availability = "Подключено" if entry["availability"] == "connected" else "Временно недоступно"
        permission = entry["permission"]
        action, label = (("capability_revoke", f'Отозвать доступ к «{entry["name"]}»') if permission == "granted"
                         else ("capability_grant", f'Разрешить доступ к «{entry["name"]}»'))
        target = f'<input type="hidden" name="capability_id" value="{key}">'
        control = form(action, label, token, data, form_id=f"capability-{key}", reason="", body=target)
        settings = "".join(
            f'<label for="{key}-{field}">{text}</label><input id="{key}-{field}" type="number" '
            f'min="1" max="86400000" name="{field}" value="{escape(str(entry["limits"].get(field, "")), quote=True)}">'
            for field, text in LIMIT_LABELS.items()
            if field != "duration_max_ms" or entry["capability_id"] != TELEGRAM_ID
        )
        limits = form("capability_limits", "Сохранить пределы", token, data, form_id=f"limits-{key}", reason="",
                      body=target + '<p class="help">Пустое поле сохраняет предел по умолчанию.</p>' + settings)
        sections.append(f'<section><h2>{name}</h2><p>{escape(entry["description"])}</p>'
                        f'<dl><dt>Доступность</dt><dd>{availability}</dd>'
                        f'<dt>Разрешение</dt><dd>{permission_names[permission]}</dd></dl>{control}'
                        f'<details><summary>Пределы использования</summary>{limits}</details></section>')
    return "".join(sections)
