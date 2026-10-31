"""D-048: необязательный будущий замысел не занимает слот текущего действия.

Ни текст цели, ни необходимость её иметь профиль не назначает. Источники,
права, версии и подтверждение результата проверяются прежним профилем.
"""
from dataclasses import replace

from iam.experiments.provenance import INSTRUCTION as PREVIOUS, ProvenanceArchitecture, ProvenanceCycle
from iam.state import KindDecision, Violation

VERSION = 'volition-v1'
OLD_LIMIT = 'Разрешено до двух memory_write и одного действия/сообщения/намерения/no_action за ответ.'
NEW_LIMIT = ('Разрешено до двух memory_write, одного intention и одного action/message за ответ. '
             'intention и action/message могут сочетаться в любом порядке. '
             'Вместо них можно выбрать одно no_action. Все эти предложения необязательны.')
INSTRUCTION = PREVIOUS.replace(OLD_LIMIT, NEW_LIMIT)


class VolitionCycle(ProvenanceCycle):
    def _context(self, trigger, focus, recalled):
        context = super()._context(trigger, focus, recalled)
        context[0] = {**context[0], 'text': INSTRUCTION}
        return context

    def _checked(self, pr, ctx):
        kinds = ctx.accepted_kinds
        if pr['kind'] == 'intention':
            if 'intention' in kinds or 'no_action' in kinds:
                return KindDecision(violation=Violation('invalid_volition', 'не более одного намерения, без no_action'))
            kinds = tuple(k for k in kinds if k == 'memory_write')
        elif pr['kind'] in ('action', 'message'):
            kinds = tuple(k for k in kinds if k != 'intention')
        return super()._checked(pr, replace(ctx, accepted_kinds=kinds))


class VolitionArchitecture(ProvenanceArchitecture):
    protocol_version = VERSION

    def __init__(self, factory, mechanism='goals'):
        super().__init__(factory, mechanism)
        self.name = 'A-' + VERSION + '-' + mechanism

    def build(self, runtime):
        cycle = super().build(runtime)
        return VolitionCycle(runtime, cycle.provider, self.mechanism)
