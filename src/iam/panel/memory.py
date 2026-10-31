"""Читаемое представление памяти; история раскрывается без изменения записей."""
import json
from html import escape

KINDS = dict(zip(
    ('sensory', 'working', 'episodic', 'semantic', 'autobiographical',
     'emotional_associative', 'procedural', 'social', 'prospective'),
    ('Сенсорная', 'Рабочая', 'Эпизодическая', 'Семантическая', 'Автобиографическая',
     'Эмоционально-ассоциативная', 'Процедурная', 'Социальная', 'Проспективная')))
ORIGINS = {'observation': 'Наблюдение', 'model_knowledge': 'Сведения модели',
           'stated_by_other': 'Чужое утверждение', 'own_hypothesis': 'Собственная гипотеза',
           'imagination': 'Воображение', 'simulation_event': 'Событие симуляции',
           'model_explanation': 'Объяснение модели'}
LAYERS = {'personal': 'Личная память', 'interpretation': 'Интерпретация'}


def _e(value):
    return escape(str(value), quote=True)


def render_memory(records):
    content = '<p>Личная память и интерпретации · только чтение. Технический архив сюда не включён.</p>'
    visible = [m for m in records if m['layer'] in LAYERS]
    if not visible:
        return content + '<p class="empty">Записей памяти пока нет. Они появятся после отбора опыта в сеансе.</p>'
    content += '<p class="help">До 50 последних записей. Источники и история доступны внутри каждой записи.</p>'
    for m in visible:
        content += (f'<article class="memory-record"><h2>{_e(KINDS[m["kind"]])}</h2>'
                    f'<p class="memory-text">{_e(m["content"]["text"])}</p>'
                    '<dl class="memory-meta">')
        fields = [('Слой', LAYERS[m['layer']]), ('Происхождение', ORIGINS[m['origin']]),
                  ('Источник', m['source']['kind'] + ': ' + m['source']['ref']),
                  ('Время записи', m['created_at']), ('Версия', m['version'])]
        content += ''.join(f'<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>' for k, v in fields)
        content += '</dl>'
        if m.get('claim_kind') == 'statement_made' or m['origin'] == 'stated_by_other':
            content += '<p class="help">Записан факт сообщения. Истинность его содержания этим не подтверждается.</p>'
        content += ('<details><summary>Источники, связи и история</summary>'
                    '<pre tabindex="0" role="region" aria-label="Источники, связи и история записи">'
                    + _e(json.dumps(m, ensure_ascii=False, indent=2)) + '</pre></details></article>')
    return content
