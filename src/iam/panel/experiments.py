"""Read-only packaged experiment archive; query parameters never name files."""
import json
from html import escape
from pathlib import Path
import re
from urllib.parse import urlencode

SNAPSHOT = Path(__file__).with_name('experiments.json')
MAX_BYTES = 1_000_000
IDENT = re.compile(r'[A-Za-z0-9_-]{1,48}')


def load_archive(path=SNAPSHOT):
    try:
        with Path(path).open('rb') as source:
            raw = source.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {'series': []}
    try:
        data = json.loads(raw) if len(raw) <= MAX_BYTES else None
        if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('series'), list):
            raise ValueError()
        ids = set()
        for series in data['series']:
            if not IDENT.fullmatch(series['id']) or series['id'] in ids:
                raise ValueError()
            ids.add(series['id'])
            if not (all(isinstance(series[k], str) for k in ('title', 'description'))
                    and isinstance(series['metrics'], list) and isinstance(series['findings'], list)
                    and isinstance(series['stories'], list)
                    and all(isinstance(item, list) and len(item) == 2 for item in series['metrics'])):
                raise ValueError()
            story_ids = set()
            for story in series['stories']:
                if not IDENT.fullmatch(story['id']) or story['id'] in story_ids:
                    raise ValueError()
                story_ids.add(story['id'])
                if not (story['moments'] and [m['number'] for m in story['moments']] == list(range(1, len(story['moments']) + 1))
                        and all(type(m['number']) is int and isinstance(m['body'], str)
                                and type(m['session']) is int and m['session'] > 0 for m in story['moments'])):
                    raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, AssertionError):
        raise ValueError('Архив результатов повреждён. Нужна повторная сборка опубликованных данных.') from None


def url(series, story=None, moment=None):
    args = {'series': series}
    if story is not None:
        args['story'] = story
    if moment is not None:
        args['moment'] = moment
    return '/experiments?' + urlencode(args) + ('#episode' if story is not None else '')


def text_blocks(text):
    """A deliberately small renderer: original text is escaped before adding tags."""
    blocks = []
    for part in text.split('\n\n'):
        if part.startswith('**') and part.endswith('**'):
            blocks.append('<h3>' + escape(part[2:-2]) + '</h3>')
        elif part.startswith('#'):
            blocks.append('<h3>' + escape(part.lstrip('# ')) + '</h3>')
        else:
            blocks.append('<p>' + escape(part).replace('\n', '<br>') + '</p>')
    return ''.join(blocks)


def render_archive(data, *, series=None, story=None, moment=None):
    entries = {entry['id']: entry for entry in data['series']}
    if not entries:
        return '<p class="empty">Архив результатов пока не установлен. После сборки архива обновите страницу.</p>'
    selected = series if series is not None else next(iter(entries))
    if selected not in entries:
        raise ValueError('Серия не найдена. Вернитесь к списку экспериментов.')
    current = entries[selected]
    stories = {item['id']: item for item in current['stories']}
    if (story is not None and story not in stories) or (moment is not None and story is None):
        raise ValueError('История не найдена. Выберите доступную биографию.')
    content = ('<p>Архив испытаний · GPT-6-Luna. Сеансы моделируемые, сообщения человека заданы сценарием. '
               'Эти истории отделены от постоянной личности.</p>'
               '<p>Для дальнейшей разработки выбран вариант A: модель предлагает решения, ядро проверяет их. '
               'Убедительная самостоятельная личность пока не получена.</p>'
               '<nav class="experiment-series" aria-label="Серии экспериментов">')
    for key, entry in entries.items():
        content += ('<a class="button" href="' + escape(url(key), quote=True) + '"' +
                    (' aria-current="page"' if key == selected else '') + '>' + escape(entry['title']) + '</a>')
    content += '</nav><section><h2>' + escape(current['title']) + '</h2><p>' + escape(current['description']) + '</p>'
    content += '<dl class="metrics">' + ''.join('<div><dt>' + escape(str(label)) + '</dt><dd>' + escape(str(value)) + '</dd></div>'
                                               for label, value in current['metrics']) + '</dl>'
    content += '<ul>' + ''.join('<li>' + escape(str(finding)) + '</li>' for finding in current['findings']) + '</ul></section>'
    if not stories:
        return content
    content += '<section><h2>Биографии по эпизодам</h2><div class="version-links">'
    for key in stories:
        content += ('<a href="' + escape(url(selected, key), quote=True) + '"' +
                    (' aria-current="page"' if key == story else '') + '>' + escape(key) + '</a>')
    content += '</div><p class="help">Выберите историю, затем один эпизод. Внутренние записи — сохранённые '
    content += 'предположения, а не прямая запись скрытого мышления. Отказы и молчание сохранены.</p></section>'
    if story is None:
        return content
    moments = stories[story]['moments']
    number = 1 if moment is None else moment
    if type(number) is not int or not 1 <= number <= len(moments):
        raise ValueError('Эпизод не найден. Выберите номер из списка.')
    choice = moments[number - 1]
    content += ('<section id="episode"><h2>' + escape(story) + ' · сеанс ' + str(choice['session']) + '</h2>'
                '<form class="experiment-choice" method="get" action="/experiments#episode">'
                '<input type="hidden" name="series" value="' + escape(selected, quote=True) + '">'
                '<input type="hidden" name="story" value="' + escape(story, quote=True) + '">'
                '<label for="experiment-moment">Эпизод<select id="experiment-moment" name="moment">')
    content += ''.join('<option value="' + str(m['number']) + '"' + (' selected' if m['number'] == number else '') +
                       '>Момент ' + str(m['number']) + ' · сеанс ' + str(m['session']) + '</option>' for m in moments)
    content += '</select></label><button class="primary" type="submit">Показать эпизод</button></form>'
    content += '<article class="experiment-episode">' + text_blocks(choice['body']) + '</article><div class="experiment-series">'
    if number > 1:
        content += '<a class="button" href="' + escape(url(selected, story, number - 1), quote=True) + '">← Предыдущий эпизод</a>'
    if number < len(moments):
        content += '<a class="button" href="' + escape(url(selected, story, number + 1), quote=True) + '">Следующий эпизод →</a>'
    return content + '</div></section>'
