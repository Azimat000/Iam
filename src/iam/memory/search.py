"""Ограниченный лексический поиск по доступным записям, без обращения к архиву."""
import math
import re


STOP = frozenset('а без бы был была были было быть в во вот все всего вы да для до его ее если есть еще же за и из или их к как ко ли мне мы на не но о об он она они оно от по при с со так то тот ты у что это я the a an and or of to is was it we you'.split())
STOP |= frozenset('про какой какая какое какие какого какую каким каких котором который которая которое которые нашу наше наши наш него нее них тебе тебя меня нас вас вам нам этим этого эту эти этой теперь сейчас'.split())


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


def diverse(ranked, scores, records, limit):
    """Близкие пересказы не занимают весь бюджет; сами записи остаются неизменными."""
    # Для сравнения свидетельств сохраняются отрицания и числа, удаляемые поисковым words().
    tokens = {mid: set(re.findall(r'[^\W_]+', records[mid].text.casefold().replace('ё', 'е'))) for mid in ranked}
    negations = {'не', 'ни', 'нет', 'нельзя', 'никогда', 'not', 'no', 'never', 'without'}
    def identity(mid):
        row = records[mid]
        content = getattr(row, 'content', {})
        communication = content.get('communication')
        return (getattr(row, 'origin', None), getattr(row, 'independence_group', None),
                getattr(row, 'claim_kind', None), content.get('outcome'),
                communication.get('person') if isinstance(communication, dict) else None,
                frozenset(tokens[mid] & negations), frozenset(t for t in tokens[mid] if t.isdecimal()))
    identities = {mid: identity(mid) for mid in ranked}
    remaining, selected = list(ranked), []
    while remaining and len(selected) < limit:
        def value(mid):
            overlaps = []
            for previous in selected:
                if identities[mid] != identities[previous]:
                    continue
                union = tokens[mid] | tokens[previous]
                overlaps.append(len(tokens[mid] & tokens[previous]) / len(union) if union else 0.0)
            return scores.get(mid, 0.0) * (1 - max(overlaps, default=0.0))
        # max сохраняет прежний порядок при равенстве: свежесть/значимость не теряются.
        best = max(remaining, key=value)
        selected.append(best)
        remaining.remove(best)
    return selected


def historical_candidates(query, records, limit, current=None):
    """До двух ранних эпизодов среди первых 2N прямых совпадений; без замены recall."""
    scores = relevance(query, records.values())
    ranked = sorted((mid for mid in scores if current not in records[mid].sources),
                    key=lambda mid: (-scores[mid], -records[mid].created_life_ms, mid))[:2 * limit]
    candidates = [mid for mid in ranked if records[mid].kind == 'episodic'
                  and records[mid].version == 1
                  and records[mid].origin in ('observation', 'stated_by_other')]
    return sorted(candidates, key=lambda mid: (records[mid].created_life_ms, ranked.index(mid)))[:2]
