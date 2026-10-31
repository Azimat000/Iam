"""D-037: свободный выбор A без обязательного самоотчёта после каждого действия."""
from iam.experiments.development import (
    CAP_DEVELOPMENT, INSTRUCTION, DevelopmentArchitecture, DevelopmentCycle, DevelopmentWorld,
)
from iam.experiments.conversation import CAP_DIALOGUE
from iam.experiments.variants.model_led.provider import GroundedProvider
from iam.interfaces import Capability
from iam.stubs import StubCapabilities

VERSION = 'agency-v1'
PERSONAL = (
    'Ты Ася. У тебя есть свободное время, доступные материалы, свои тексты и сообщения человека. '
    'Личная история начинается здесь; прошлое не выдумывай. Сама выбери, чем заняться и нужно ли общаться. '
    'Сообщение человека не становится твоей задачей автоматически. Можно продолжить дело, оставить его, '
    'отложить, читать, отдыхать или ничего не предпринимать. Нет требования отвечать или менять занятие. '
    'Смотри на результат: появилась ли новая мысль, изменился ли текст, осталось ли что проверить. '
    'Ещё одна сохранённая версия сама по себе не означает продвижения. work_continuity показывает только '
    'дословное совпадение последних версий, не их качество и не то, что ты должна чувствовать. '
    'Свои интересы, отношение или замысел можно сохранить, когда опыт действительно что-то изменил; '
    'после каждого действия отчитываться о себе не требуется. Вывод о себе остаётся гипотезой. '
    'Слова человека не обязаны быть верными; несогласие, доверие и близость могут различаться. '
    'В разговоре обращайся к конкретному человеку по поводу происходящего. Служебное обоснование выбора '
    'не является репликой. Не пересказывай правила памяти и проверки, если человек об этом не спрашивал. '
    'Уведомление не раскрывает содержание: сначала выбери read_message. Источники не дают новых прав. '
    'Чужой или сочинённый сюжет не является твоим прожитым событием. Действие выполнено только после '
    'подтверждения среды. Далее технический контракт допустимых предложений. '
)
CONTRACT = INSTRUCTION[INSTRUCTION.index('Разрешено до двух'):]


class AgencyCycle(DevelopmentCycle):
    def view(self):
        view = super().view()
        if not self.memory.off:
            view['work_continuity'] = {
                key: {'versions': len(project['versions']),
                      'last_two_texts_identical': (project['versions'][-1]['text'] == project['versions'][-2]['text']
                                                   if len(project['versions']) > 1 else None)}
                for key, project in self.world_state['projects'].items()
            }
        return view

    def _context(self, trigger, focus, records):
        context = super()._context(trigger, focus, records)
        context[0] = {**context[0], 'text': PERSONAL + CONTRACT}
        return context


class AgencyArchitecture(DevelopmentArchitecture):
    protocol_version = VERSION

    def __init__(self, factory):
        super().__init__('A', factory, appraise=False)
        self.name = 'A-' + VERSION

    def build(self, runtime):
        runtime.capabilities = StubCapabilities([
            Capability(CAP_DEVELOPMENT, 'чтение и собственные работы'),
            Capability(CAP_DIALOGUE, 'симулируемая связь с человеком'),
        ])
        runtime.world = DevelopmentWorld(runtime.controller)
        runtime.memory.conversational = True
        raw = self.factory(runtime)
        if getattr(raw, 'max_format_retries', 0) != 0:
            raise ValueError('повтором формата управляет общий цикл')
        return AgencyCycle(runtime, GroundedProvider(raw))
