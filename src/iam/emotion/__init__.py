"""Три масштаба и регуляция: ограниченные инженерные сигналы, предложенные A."""
import math

REACTION_HALF_MS = 60000
MOOD_HALF_MS = 3600000


def bounded(value, low=0.0, high=1.0):
    return min(high, max(low, value)) if type(value) in (int, float) and (type(value) is int or math.isfinite(value)) else 0.0


def decay(value, elapsed, half_life):
    return value * 2 ** (-max(0, elapsed) / half_life)


def current(saved, now):
    """Календарное ожидание и пауза не входят в elapsed."""
    elapsed = max(0, now - saved['started_life_ms'])
    return {**saved, 'reaction': decay(saved['reaction'], elapsed, REACTION_HALF_MS),
            'expressed': decay(saved['expressed'], elapsed, REACTION_HALF_MS),
            'mood': decay(saved['mood'], elapsed, MOOD_HALF_MS)}


def appraise(spec, state, previous, past, now):
    """Факт события не задаёт знак: valence/relevance предлагает модель с основаниями."""
    traits = state['traits']
    sensitivity = traits.get('sensitivity', {})
    sensitivity = sensitivity.get('value', 0.5) if isinstance(sensitivity, dict) else 0.5
    disposition = traits.get('irritability', {})
    disposition = bounded(disposition.get('value', 0.0)) if isinstance(disposition, dict) else 0.0
    sensitivity = bounded(sensitivity)
    mood = previous['mood'] if previous else 0.0
    goals = min(1.0, len(state['intentions']) / 4) * spec['relevance']
    load = bounded(state['affect'].get('load'))
    reaction = bounded(spec['valence'] * (0.6 + 0.4 * sensitivity + 0.1 * goals)
                       + 0.1 * mood + 0.1 * past - 0.1 * load - 0.1 * disposition, -1, 1)
    return {'reaction': reaction, 'mood': bounded(0.9 * mood + 0.1 * reaction, -1, 1),
            'arousal': bounded(spec['arousal'] + 0.1 * abs(reaction)),
            'dispositions': {'irritability': disposition},
            'impulse': reaction, 'expressed': reaction * (1 - spec['regulation']),
            'started_life_ms': now,
            'trace': {'valence': spec['valence'], 'sensitivity': sensitivity, 'goals': goals,
                      'past': past, 'disposition': disposition, 'prior_mood': mood, 'load': load, 'regulation': spec['regulation']}}


__all__ = ['appraise', 'current']
