"""Серверный HTML: всё содержимое базы экранируется, архив не читается."""
import json
from html import escape

NAV = {"": "Сеанс", "events": "События", "state": "Состояние", "memory": "Память",
       "requests": "Просьбы к вам", "capabilities": "Возможности", "experiments": "Эксперименты",
       "usage": "Расход и ошибки", "backups": "Копии и восстановление"}
STATUSES = {"running": "Идёт", "paused": "На паузе", "stopped": "Не запущен"}
REASONS = {"operator": "По команде оператора", "session_limit": "Достигнут лимит сеанса",
           "auth_lost": "Нужен вход или исчерпан лимит подписки", "repeated_failure": "Повторный сбой исполнителя",
           "crash": "Восстановлен после аварии"}


def e(value):
    return escape(str(value), quote=True)


def dump(value):
    return '<pre tabindex="0" role="region" aria-label="Сохранённые данные">' + e(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'


def duration(ms):
    return f"{ms // 60_000:02d}:{ms // 1000 % 60:02d}"


def form(action, label, token, data, *, body="", reason=None, primary=False, values=None):
    if data["sending"]:
        return ''
    values = values or {}
    reason = values.get("reason", reason or label)
    return (f'<form method="post" action="/actions/{action}">'
            f'<input type="hidden" name="csrf" value="{e(token)}">'
            f'<input type="hidden" name="version" value="{data["version"]}">'
            f'<input type="hidden" name="epoch" value="{data["epoch"]}">'
            f'{body}<label for="reason-{action}">Причина действия</label>'
            f'<input id="reason-{action}" name="reason" value="{e(reason)}" maxlength="500" required>'
            f'<button type="submit" class="{"primary" if primary else "secondary"}">{e(label)}</button></form>')


def event_rows(events):
    if not events:
        return '<p class="empty">Событий пока нет. Они появятся после запуска сеанса.</p>'
    return '<ol class="events">' + ''.join(
        f'<li><div class="event-meta"><span>#{ev["seq"]} · {e(ev["occurred_at"])}</span>'
        f'<span>версия {ev["state_version"]}</span></div><details><summary>{e(ev["type"])}</summary>'
        f'{dump(ev)}</details></li>' for ev in events) + '</ol>'


def render(page, data, token, *, error=None, notice=None, values=None):
    u, state = data["usage"], data["session_state"]
    values = values or {}
    notice_html = f'<div class="notice" role="status">{e(notice)}</div>' if notice else ''
    if data["sending"]:
        notice_html += '<p class="notice" role="status">Telegram завершает отправку. Наблюдение доступно; команды появятся после записи исхода.</p>'
    error_html = f'<div class="error" role="alert"><h2>Действие не выполнено</h2><p>{e(error)}</p></div>' if error else ''
    if page == "":
        remaining = max(0, u["max_ms"] - u["elapsed_ms"])
        content = (f'<div class="session-title"><h2>{STATUSES[state]}</h2></div>'
                   f'<p>{e(REASONS.get(data["pause_reason"], "Сеанс ограничен временем и числом обращений к модели."))}</p>'
                   f'<dl class="metrics"><div><dt>Время сеанса</dt><dd>{duration(u["elapsed_ms"])}</dd></div>'
                   f'<div><dt>Осталось времени</dt><dd>{duration(remaining)}</dd></div>'
                   f'<div><dt>Обращения</dt><dd>{u["calls"]} <span>/ {u["max_calls"]}</span></dd></div></dl>')
        if state == "stopped" and u["session_id"]:
            content += '<p class="help">Выше — итоги последнего сеанса. Лимиты нового сеанса задаются ниже.</p>'
        if u["in_flight"]:
            content += '<p class="notice">Модель отвечает. Пауза доступна сейчас; ответ, пришедший после неё, проверит ядро.</p>'
        if state == "stopped" and not u["in_flight"]:
            body = (f'<div class="fields"><div><label for="minutes">Минуты (1–30)</label>'
                    f'<input id="minutes" name="minutes" type="number" min="1" max="30" value="{e(values.get("minutes",30))}" required></div>'
                    f'<div><label for="calls">Обращения (1–10)</label><input id="calls" name="calls" type="number" min="1" max="10" '
                    f'value="{e(values.get("calls",10))}" required></div></div>')
            content += '<section><h2>Новый сеанс</h2>' + form("start", "Запустить сеанс", token, data, body=body, primary=True, values=values) + '</section>'
        elif state == "running":
            content += '<div class="actions">' + form("pause", "Поставить на паузу", token, data, primary=True, values=values)
            content += form("stop", "Завершить сеанс", token, data, values=values) + '</div>'
        elif state == "paused":
            exhausted = u["calls"] >= u["max_calls"] or remaining == 0
            content += '<div class="actions">'
            if not exhausted and not u["in_flight"]:
                content += form("resume", "Продолжить сеанс", token, data, primary=True, values=values)
            else:
                content += '<p>Продолжение недоступно: дождитесь ответа модели или завершите сеанс с исчерпанным лимитом.</p>'
            content += form("stop", "Завершить сеанс", token, data, values=values) + '</div>'
        content += ('<p class="help">В паузе время жизни стоит. Лимит обращений сохраняется после перезапуска. Это лимиты приложения, а не квота подписки. '
                    'Для нового бюджета завершите сеанс и запустите следующий. Все команды записываются как вмешательства оператора.</p>'
                    '<section><h2>Последние события</h2>' + event_rows(data["events"][:5]) + '</section>')
    elif page == "events":
        content = '<p>Сначала новые события. Раскройте строку, чтобы увидеть её источник и содержание.</p>' + event_rows(data["events"])
        if data["events"]:
            content += f'<a class="button" href="/events?before={data["events"][-1]["seq"]}">Более ранние события</a>'
    elif page == "state":
        content = '<p>Только чтение. Выберите сохранённую версию состояния.</p><div class="version-links">'
        content += ''.join(f'<a href="/state?version={v["state_version"]}">Версия {v["state_version"]}</a>'
                           for v in data["history"] if v["epoch"] == data["epoch"])
        content += '</div>' + dump({"view_version": data["view_version"] or data["version"], "state": data["state"]})
    elif page == "memory":
        content = '<p>Личная память и интерпретации · только чтение. Технический архив сюда не включён.</p>'
        content += ''.join(f'<article><h2>{e(m["kind"])}</h2>{dump(m)}</article>' for m in data["memories"]) or '<p>Записей памяти пока нет. Они появятся после подтверждённых событий.</p>'
    elif page == "requests":
        content = '<p>Просьбы личности и сохранённые статусы. Ответы через панель появятся на этапе S23.</p>'
        content += event_rows(data["requests"]) + dump(data["state"]["world_time"].get("world", {}).get("creator_requests", {}))
    elif page == "capabilities":
        content = '<p>Доступные средства · только чтение. Изменение прав появится на этапе S23.</p>'
        content += dump(data["capabilities"]) if data["capabilities"] else '<p>Внешние действия не подключены.</p>'
    elif page == "experiments":
        content = '<p class="empty">Стенд экспериментов появится на этапе S11. Сравнительные результаты — после S16.</p>'
    elif page == "usage":
        content = (f'<p>Обращений в текущем сеансе: {u["calls"]} из {u["max_calls"]}. Время: {duration(u["elapsed_ms"])}.</p>'
                   '<p>Учитываются запуски исполнителя, включая ошибки и повторы формата. Денежные оценки для подписки не рассчитываются.</p>'
                   '<h2>Telegram</h2>' + (f'<p>Последний успешный опрос: {e(data["last_poll"])}</p>' if data["last_poll"] else '<p>Успешного опроса ещё не было.</p>'))
        if data["poll_stale"]:
            content += '<p class="notice">Опрос Telegram не подключён или давно не обновлялся. Telegram хранит входящие не дольше 24 часов.</p>'
        content += '<h2>Исходящие сообщения</h2>' + dump(data["outbox"]) + '<h2>Ошибки и лимиты</h2>' + event_rows(data["errors"])
    else:  # backups
        content = '<p>Хранятся три последние копии. Восстановление создаёт отдельную базу и сохраняет текущую.</p>'
        content += form("backup", "Создать копию", token, data, primary=True, values=values)
        if data["broken_backups"]:
            content += f'<p class="error">Повреждённых копий: {data["broken_backups"]}. Они не удалены автоматически.</p>'
        if not data["backups"]:
            content += '<p class="empty">Копий пока нет. Создайте первую копию кнопкой выше.</p>'
        for b in data["backups"]:
            content += f'<article><h2>Версия {b["version"]}</h2><p>{e(b["created_at"])} · {e(b["id"])}</p>'
            if state != "running" and not u["in_flight"]:
                body = (f'<input type="hidden" name="backup_id" value="{e(b["id"])}">'
                        '<label class="check"><input type="checkbox" name="confirm" value="copy" required> Создать отдельную базу из этой копии</label>')
                # Каждая форма имеет уникальные id, даже при нескольких копиях.
                content += form("restore", "Восстановить в отдельную базу", token, data, body=body, values=values).replace('reason-restore', 'reason-' + e(b['id']))
            else:
                content += '<p>Для восстановления поставьте сеанс на паузу и дождитесь ответа модели.</p>'
            content += '</article>'
    nav = ''.join(f'<a href="/{key}" {"aria-current=page" if key == page else ""}>{label}</a>' for key, label in NAV.items())
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow"><title>{NAV[page]} · Iam</title><link rel="stylesheet" href="/style.css"></head>'
            '<body><a class="skip" href="#main">К содержанию</a><div class="shell"><aside><a class="brand" href="/">Iam<span>Панель оператора</span></a>'
            f'<nav aria-label="Разделы панели">{nav}</nav><p class="local">Только этот компьютер<br>127.0.0.1</p></aside>'
            f'<main id="main"><header><div><p class="context">{"Эксперимент · тестовая модель" if data["demo"] else "Claude Code · подписка"}</p>'
            f'<h1>{NAV[page]}</h1></div><a class="button" href="/{page}">Обновить</a></header>'
            f'<p class="identity">{e(data["persona_id"])} · эпоха {data["epoch"]} · версия {data["version"]} · время жизни {duration(data["life_ms"])}</p>'
            f'{notice_html}{error_html}{content}<footer>Данные на момент открытия страницы. Обновление — по кнопке «Обновить».</footer></main></div></body></html>')
