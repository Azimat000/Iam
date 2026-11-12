"""Технический сигнал нагрузки по времени жизни, не модель человеческой усталости."""
from iam.emotion import bounded

WORK_MS = 20 * 60 * 1000
REST_MS = 10 * 60 * 1000


def advance(affect, intentions, now):
    attention = affect.get('attention', {})
    previous = affect.get('load_clock')
    effort = 0.0
    if isinstance(previous, dict):
        effort = bounded(previous.get('effort'))
        start = min(now, previous['life_ms'])
        focus = previous['focus']
        effort = bounded(effort + (now - start) * (-1 / REST_MS if focus == 'rest' else 1 / WORK_MS))
    goals = sum(isinstance(g, dict) and g.get('status') in
                ('active', 'deferred', 'waiting_opportunity') for g in intentions.values())
    value = bounded(.8 * effort + .2 * min(1, goals / 4))
    return value, {'life_ms': now, 'effort': effort, 'focus': previous['focus'] if isinstance(previous, dict) else attention.get('focus', 'rest')}
