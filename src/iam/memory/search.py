"""Ограниченный лексический поиск по доступным записям, без обращения к архиву."""
import math
import re


STOP = frozenset('а без бы был была были было быть в во вот все всего вы да для до его ее если есть еще же за и из или их к как ко ли мне мы на не но о об он она они оно от по при с со так то тот ты у что это я the a an and or of to is was it we you'.split())


def words(text):
    return {w for w in re.findall(r'[^\W_]+', text.casefold().replace('ё', 'е'))
            if (len(w) > 1 or w.isdecimal()) and w not in STOP}


def match(a, b):
    if a == b:
        return 1.0
    # Только близкие словоформы: общий длинный префикс, а не произвольная подстрока.
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return .75 if common >= 4 and common / max(len(a), len(b)) >= .75 else 0.0


def relevance(query, records):
    """Редкие совпадающие слова важнее свежести. Это поиск кандидатов, не вывод об их истинности."""
    terms = sorted(words(query))[:64]
    if not terms:
        return {}
    documents = {r.memory_id: words(r.text) for r in records}
    scores = dict.fromkeys(documents, 0.0)
    for term in terms:
        hits = {mid: max((match(term, word) for word in tokens), default=0.0)
                for mid, tokens in documents.items()}
        frequency = sum(value > 0 for value in hits.values())
        weight = math.log(1 + (len(documents) + 1) / (frequency + 1))
        for mid, value in hits.items():
            scores[mid] += value * weight
    return {mid: score for mid, score in scores.items() if score > 0}
