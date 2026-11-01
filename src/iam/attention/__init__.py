"""Ограниченные сигналы внимания для выбранной модельной архитектуры A."""
import math

from iam.cycle.attention import AttentionPolicy as ScaffoldPolicy, Focus

CHOICES = ('activity', 'intention', 'event', 'dialogue', 'rest')
NOTICE_CAP = 0.25


def unit(value):
    """Недоверенные/будущие признаки не создают NaN или неограниченный вес."""
    return min(1.0, max(0.0, value)) if type(value) in (int, float) and math.isfinite(value) else 0.0


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

    def signals(self, state, notice):
        affect = state['affect']
        current = affect.get('attention', {})
        current = current if isinstance(current, dict) else {}
        history = switch_history(state)
        senders = {item['sender'] for item in notice.get('items', [])}
        closeness = max((unit(state['relations'].get(sender, {}).get('closeness')) for sender in senders
                         if isinstance(state['relations'].get(sender), dict)), default=0.0)
        count = notice['count']
        return {'choices': list(CHOICES), 'current_focus': current.get('focus') if current.get('focus') in CHOICES else None,
                'notice_signal': min(NOTICE_CAP, count / (count + 4) * NOTICE_CAP),
                'notice_cap': NOTICE_CAP, 'goal_signal': min(1.0, len(state['intentions']) / 4),
                'load': unit(affect.get('load')), 'relationship_signal': closeness,
                'switch_cost': min(1.0, len(history) / 4),
                'rule': 'Сигналы помогают выбору; максимальное число не назначает действие. '
                        'Можно продолжить дело, сменить занятие, прочесть, ждать или бездействовать.'}


__all__ = ['AttentionPolicy', 'CHOICES', 'NOTICE_CAP']
