"""Ограниченные сигналы внимания для выбранной модельной архитектуры A."""
import math

from iam.cycle.attention import AttentionPolicy as ScaffoldPolicy, Focus
from iam.domain.contacts import contact_id

CHOICES = ('activity', 'intention', 'event', 'dialogue', 'rest')
NOTICE_CAP = 0.25
SWITCH_WINDOW_MS = 300_000


def unit(value):
    """Недоверенные/будущие признаки не создают NaN или неограниченный вес."""
    return min(1.0, max(0.0, value)) if type(value) in (int, float) and (type(value) is int or math.isfinite(value)) else 0.0


def switch_history(state):
    current = state['affect'].get('attention', {})
    history = current.get('switch_history') if isinstance(current, dict) else None
    return [v for v in history if type(v) is int and v >= 0][-16:] if isinstance(history, list) else []


class AttentionPolicy(ScaffoldPolicy):
    """Сохраняет текущий фокус; модель свободна выбрать любой из пяти вариантов."""

    def choose(self, trigger, state):
        current = state['affect'].get('attention', {})
        if isinstance(current, dict) and current.get('focus') in CHOICES:
            return Focus(current['focus'], current.get('ref'))
        return super().choose(trigger, state)

    def signals(self, state, notice, *, now_ms=0):
        affect = state['affect']
        current = affect.get('attention', {})
        current = current if isinstance(current, dict) else {}
        history = switch_history(state)
        switching = sum(1 - (now_ms - at) / SWITCH_WINDOW_MS for at in history
                        if 0 <= now_ms - at < SWITCH_WINDOW_MS)
        open_goals = sum(isinstance(goal, dict) and goal.get('status', 'active')
                         in ('active', 'deferred', 'waiting_opportunity') for goal in state['intentions'].values())
        # Старые тестовые/внешние уведомления без канала уже содержали ключ отношения.
        senders = {contact_id(item['channel'], item['sender']) if 'channel' in item else item['sender']
                   for item in notice.get('items', [])}
        closeness = max((unit(state['relations'].get(sender, {}).get('closeness')) for sender in senders
                         if isinstance(state['relations'].get(sender), dict)), default=0.0)
        count = notice['count']
        return {'choices': list(CHOICES), 'current_focus': current.get('focus') if current.get('focus') in CHOICES else None,
                'notice_signal': min(NOTICE_CAP, count / (count + 4) * NOTICE_CAP),
                'notice_cap': NOTICE_CAP, 'goal_signal': min(1.0, open_goals / 4),
                'load': unit(affect.get('load')), 'relationship_signal': closeness,
                'switch_cost': min(1.0, switching / 4), 'switch_window_ms': SWITCH_WINDOW_MS,
                'emotion_signal': unit(abs(affect.get('emotion', {}).get('reaction', 0))),
                'mood_signal': affect.get('emotion', {}).get('mood', 0),
                'rule': 'Сигналы помогают выбору; максимальное число не назначает действие. '
                        'Можно продолжить дело, сменить занятие, прочесть, ждать или бездействовать.'}


__all__ = ['AttentionPolicy', 'CHOICES', 'NOTICE_CAP']
