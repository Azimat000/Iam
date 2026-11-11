"""Точный ограниченный объём текста; продолжение открывается отдельным выбором."""
import json


def text_window(metadata, text, limit, start=0):
    def encoded(end):
        return json.dumps(dict(metadata, text=text[start:end],
                              text_window={'start': start, 'end': end, 'total': len(text),
                                           'more': end < len(text)}), ensure_ascii=False)

    if len(encoded(start)) > limit:
        return None
    low, high = start, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(encoded(mid)) <= limit:
            low = mid
        else:
            high = mid - 1
    return encoded(low)


def next_window(record, context):
    """Только текущая доступная версия и показанное окно; архив не используется."""
    for item in context:
        if item['ref'] != record.memory_id or item['layer'] != record.layer:
            continue
        try:
            data = json.loads(item['text'])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get('version') != record.version:
            continue
        window = data.get('text_window', {})
        if not isinstance(window, dict):
            continue
        end = window.get('end')
        if type(end) is int and 0 < end <= len(record.text):
            return end if end < len(record.text) else 0
    return None
