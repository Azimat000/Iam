"""Страница наблюдения мира и явная загрузка предоставленного оператором текста."""
from html import escape

STATUSES = {'active':'В работе', 'waiting_opportunity':'Ждёт возможности', 'deferred':'Отложено',
            'done':'Выполнено', 'abandoned':'Оставлено'}


def e(value):
    return escape(str(value), quote=True)


def render_world(data, token, form, dump, *, values=None):
    values = values or {}
    world = data['state']['world_time'].get('world', {})
    content = '<p>Личное пространство, библиотека, собственные работы и намерения. Здесь можно наблюдать за занятиями и добавить текст для чтения.</p>'
    content += '<section><h2>Библиотека</h2>'
    library = world.get('library', {})
    if not library:
        content += '<p class="empty">В библиотеке пока нет текстов. Добавьте первый материал через форму ниже.</p>'
    for key, item in library.items():
        content += (f'<article><h3>{e(item["title"])}</h3><p>{"Прочитано" if item["read"] else "Ещё не прочитано"} · текст от оператора</p>'
                    f'<details><summary>Текст материала</summary><pre tabindex="0">{e(item["text"])}</pre></details>'
                    f'<p class="help">{e(key)}</p></article>')
    content += '<h3>Добавить текст в библиотеку</h3>'
    if data['session_state'] == 'running' or data['usage']['in_flight'] or data['sending']:
        content += '<p>Для загрузки <a href="/">поставьте сеанс на паузу</a> и дождитесь ответа модели. Затем обновите эту страницу.</p>'
    else:
        body = ('<label for="material-title">Название материала</label>'
                f'<input id="material-title" name="title" value="{e(values.get("title", ""))}" maxlength="160" required>'
                '<label for="material-content">Текст для чтения</label>'
                f'<textarea id="material-content" name="content" rows="7" maxlength="8000" aria-describedby="material-help" required>{e(values.get("content", ""))}</textarea>'
                '<p id="material-help" class="help">До 8000 знаков. Добавление сохраняется в журнале; чтение остаётся выбором личности. Не вставляйте пароли и токены.</p>')
        content += form('material_add', 'Добавить текст', token, data, body=body, reason='', primary=True, values=values)
    content += '</section><section><h2>Личное пространство</h2>'
    space = world.get('space', {})
    objects = space.get('objects', {})
    if not objects:
        content += '<p class="empty">В пространстве пока нет предметов.</p>'
    else:
        content += '<dl>' + ''.join(f'<dt>{e(item["title"])}</dt><dd>{e(space["places"].get(item["place_id"],item["place_id"]))}</dd>'
                                   for item in objects.values()) + '</dl>'
    content += '</section><section><h2>Собственные работы</h2>'
    projects = world.get('projects', {})
    if not projects:
        content += '<p class="empty">Работ пока нет. Они появятся, когда личность решит что-нибудь написать и мир подтвердит сохранение.</p>'
    for key, project in projects.items():
        content += f'<article><h3>{e(project["title"])}</h3><p class="help">Проект {e(key)}</p>'
        works = [(aid, work) for aid, work in world.get('works', {}).items()
                 if world['actions'][aid]['params']['project_id'] == key]
        for aid, work in reversed(works):
            content += (f'<details><summary>Сохранённая работа · {e(aid)}</summary><pre tabindex="0">{e(work["text"])}</pre>'
                        f'<p class="help">Подтверждение: {e(work["event_id"])}. Сохранение текста само по себе не подтверждает выполнение цели.</p></details>')
        content += '</article>'
    content += '</section><section><h2>Намерения</h2>'
    goals = data['state']['intentions']
    if not goals:
        content += '<p class="empty">Намерений пока нет. Личность может создать собственный замысел по своему опыту; обязательства иметь цель нет.</p>'
    for key, goal in goals.items():
        content += (f'<article><h3>{e(goal.get("text",key))}</h3><p>{e(STATUSES.get(goal["status"],goal["status"]))}</p>'
                    f'<p><strong>Исходный критерий:</strong> {e(goal.get("criterion","Не задан в старом формате"))}</p>'
                    f'<p class="help">Знает о действии: {"да" if goal.get("knows") else "не указано"}. '
                    f'Хочет: {"да" if goal.get("wants") else "не указано"}. Право на действие проверяется отдельно при каждой попытке.</p>'
                    '<details><summary>Основания и история</summary>' + dump(goal) + '</details></article>')
    return content + '</section>'
