"""Доступность подключённых исполнителей и сохранённые разрешения оператора."""

from dataclasses import replace

from iam.domain import Op
from iam.storage import NotFound

REGISTRY_PATH = "capability_permissions"
TELEGRAM_ID = "cap_Telegram01"
LIMIT_FIELDS = ("max_uses_per_session", "max_uses_per_period", "period_life_ms", "duration_max_ms")
DEFAULT_PERMISSIONS = {"needs_permission": "required", "revoked": "revoked"}


def parse_limits(fields):
    try:
        limits = {key: int(fields[key]) for key in LIMIT_FIELDS if fields.get(key)}
    except (TypeError, ValueError) as error:
        raise ValueError("Введите пределы целыми числами.") from error
    if any(not 1 <= number <= 86_400_000 for number in limits.values()):
        raise ValueError("Пределы должны быть целыми числами от 1 до 86 400 000.")
    if ("max_uses_per_period" in limits) != ("period_life_ms" in limits):
        raise ValueError("Для предела за период нужны количество использований и длительность периода.")
    return limits


class Registry:
    def __init__(self, controller, capabilities):
        self.ctl = controller
        self._tools = {tool.capability_id: tool for tool in capabilities}

    def get(self, capability_id):
        return self._effective(capability_id, self._permissions())

    def list(self):
        permissions = self._permissions()
        return [self._effective(key, permissions) for key in self._tools]

    def _permissions(self):
        return self.ctl.state["world_time"].get(REGISTRY_PATH, {})

    def _effective(self, key, permissions):
        tool = self._tools.get(key)
        if tool is None:
            return None
        setting = permissions.get(key, {})
        permission = setting.get("status", DEFAULT_PERMISSIONS.get(tool.status, "granted"))
        if permission == "revoked":
            status = "revoked"
        elif tool.status == "unavailable_temporarily":
            status = tool.status
        else:
            status = "available" if permission == "granted" else "needs_permission"
        return replace(tool, status=status, limits={**tool.limits, **setting.get("limits", {})})

    def entries(self):
        permissions = self._permissions()
        return [
            {
                **self._effective(key, permissions).__dict__,
                "availability": "unavailable" if tool.status == "unavailable_temporarily" else "connected",
                "permission": permissions.get(key, {}).get(
                    "status", DEFAULT_PERMISSIONS.get(tool.status, "granted")),
            }
            for key, tool in self._tools.items()
        ]

    def execution_violation(self, capability_id, body):
        capability = self.get(capability_id)
        if capability is None or not capability.available:
            return "Возможность сейчас недоступна."
        return self.limit_violation(capability, body, self.ctl.session_id, self.ctl.life_time_ms)

    def limit_violation(self, capability, body, session_id, now):
        limits = capability.limits
        duration = limits.get("duration_max_ms")
        if duration is not None and "params" in body and body.get("expected_duration_ms", duration + 1) > duration:
            return "Укажите продолжительность в пределах разрешённого времени."
        scope = limits.get("scope", {})
        params = body.get("params", {}) | ({"to": body["to"]} if "to" in body else {})
        if any(params.get(field) not in allowed for field, allowed in scope.items()):
            return "Параметры выходят за пределы разрешённой области."
        session_max = limits.get("max_uses_per_session")
        period_max = limits.get("max_uses_per_period")
        if session_max is None and period_max is None:
            return None
        attempts = self._attempts(capability.capability_id)
        if session_max is not None and sum(event.payload.get("session_id") == session_id for event in attempts) >= session_max:
            return "Исчерпан предел использований в сеансе."
        if period_max is not None and sum(event.life_time_ms > now - limits["period_life_ms"]
                                          for event in attempts) >= period_max:
            return "Исчерпан предел использований за период."
        return None

    def _attempts(self, capability_id):
        from iam.creator_bridge import delivery_id

        attempts = []
        for event in self.ctl.events_after(0):
            if (event.type in ("action.attempted", "message.out_requested")
                    and event.payload.get("capability_id") == capability_id):
                attempts.append(event)
            elif capability_id == TELEGRAM_ID and event.type == "creator.request" and event.payload.get("crq_id"):
                try:
                    self.ctl.store.get_outbox(delivery_id(event.payload["crq_id"]))
                except NotFound:
                    continue
                attempts.append(event)
        return attempts

    def set_permission(self, capability_id, *, grant, reason):
        if capability_id not in self._tools:
            raise ValueError("Возможность не найдена. Обновите страницу.")
        status = "granted" if grant else "revoked"
        current = next(entry for entry in self.entries() if entry["capability_id"] == capability_id)
        if current["permission"] == status:
            raise ValueError("У возможности уже такое разрешение.")
        result = self.ctl.intervene(
            "grant_capability" if grant else "revoke_capability",
            reason,
            (Op("world_time", f"{REGISTRY_PATH}.{capability_id}.status", "set", status, True),),
            target=capability_id,
        )
        if not result.applied:
            raise ValueError("Разрешение не сохранено. Проверьте журнал.")
        return result

    def set_limits(self, capability_id, fields, *, reason):
        if capability_id not in self._tools:
            raise ValueError("Возможность не найдена. Обновите страницу.")
        limits = parse_limits(fields)
        if capability_id == TELEGRAM_ID and "duration_max_ms" in limits:
            raise ValueError("У Telegram можно ограничить количество отправок.")
        result = self.ctl.intervene("set_capability_limits", reason, (
            Op("world_time", f"{REGISTRY_PATH}.{capability_id}.limits", "set", limits, True),),
            target=capability_id)
        if not result.applied:
            raise ValueError("Пределы не сохранены. Проверьте журнал.")
        return result
