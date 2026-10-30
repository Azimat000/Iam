"""Вариант A: модель предлагает, общий StateController проверяет и записывает."""
from iam.cycle.cycle import DEFAULT_ALLOWED
from iam.experiments.runtime import ExperimentCycle

from .context import build_context, repair_context
from .provider import GroundedProvider


def state_boundary(check):
    """Техническое состояние стенда не является психологическим решением модели.

    Разрешённые записи действий/просьб порождают общие разборщики ядра. Прямой state_change
    этих двух разделов уже запрещён самим StateController (RESERVED_MODEL_PATHS).
    """
    if check.actor == "model" and any(
            c["group"] == "world_time" and not c["path"].startswith(("world.actions.", "world.creator_requests."))
            for c in check.changes):
        return ["вариант A не меняет техническое состояние мира через state_change"]
    return []


class ModelLedCycle(ExperimentCycle):
    def _context(self, trigger, focus, records):
        return build_context(super()._context(trigger, focus, records), trigger, self.ctl.state)

    def _request(self, cid, context, attempt, retry_of):
        return super()._request(cid, repair_context(context) if attempt else context, attempt, retry_of)


class ModelLedArchitecture:
    name = "A-model-led"

    def __init__(self, provider_factory):
        """Фабрика (runtime -> CognitiveProvider) создаёт отдельного исполнителя на каждый прогон.

        Для ClaudeCodeProvider: max_format_retries=0, поскольку одну попытку исправления ведёт Cycle.
        Живой провайдер подключается явно; по умолчанию сетевых обращений нет.
        """
        self.provider_factory = provider_factory

    def build(self, runtime):
        provider = self.provider_factory(runtime)
        if getattr(provider, "max_format_retries", 0) != 0:
            raise ValueError("повтор формата ведёт цикл: max_format_retries исполнителя должен быть 0")
        runtime.controller.add_validator("model_led_state_boundary", state_boundary)
        return ModelLedCycle(runtime, GroundedProvider(provider), max_rerequest=0,
                             allowed_proposals=(*DEFAULT_ALLOWED, "state_change"))
