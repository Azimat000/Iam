"""Формат и основания A; окончательное решение о применении всегда у ядра."""
import copy

from iam.cycle.validator import tool_use_signs
from iam.domain import jsonschema
from iam.interfaces import ProviderError
from iam.providers.errors import ProviderFormatError


class GroundedProvider:
    name = "model-led"

    def __init__(self, provider):
        self.provider = provider

    def propose(self, request):
        # Провайдер не получает ссылку на объект, уже записанный/используемый циклом.
        try:
            response = self.provider.propose(copy.deepcopy(request))
        except ProviderFormatError:
            return {"model_led_error": "bad_format"}
        except Exception:
            # В Cycle исключение попадает в журнал: сырой stderr/текст ответа туда не передаём.
            raise ProviderError("исполнитель варианта A недоступен; решение не получено") from None
        if jsonschema.errors("response", response):
            return {"model_led_error": "bad_format"}
        if response["tool_use_detected"] or tool_use_signs(response) or response["status"] != "ok":
            return response  # отказ/опасный ответ разбирает ядро; исправлением его не маскируем
        refs = {item["ref"] for item in request["context"] if item["ref"].startswith(("evt_", "mem_"))}
        if any(not pr["cites"] or not set(pr["cites"]) <= refs for pr in response["proposals"]):
            return {"model_led_error": "ungrounded_cites"}
        return response
