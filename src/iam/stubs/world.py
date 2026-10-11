"""StubWorld — мир на бумаге: ничего не делает, но по сценарию отвечает «подтверждено», «не вышло»,
«неизвестно» или «обрыв». Все полученные просьбы лежат в `executed`."""
from __future__ import annotations

from iam.interfaces import ActionOutcome, ActionRequest

BEHAVIOURS = ("confirm", "fail", "unknown", "crash")


class WorldCrash(Exception):
    """Процесс мира оборвался посреди действия: исход неизвестен."""


class StubWorld:
    def __init__(self, default: str = "confirm"):
        self._check(default)
        self.default = default
        self.executed: list[ActionRequest] = []
        self._plan: dict[str, list[str]] = {}

    @staticmethod
    def _check(behaviour: str) -> None:
        if behaviour not in BEHAVIOURS:
            raise ValueError(f"поведение {behaviour!r}: нужно одно из {BEHAVIOURS}")

    def will(self, capability_id: str, *behaviours: str) -> "StubWorld":
        """Очередь поведений для возможности (по одному на вызов); когда кончится — поведение по умолчанию."""
        for b in behaviours:
            self._check(b)
        self._plan.setdefault(capability_id, []).extend(behaviours)
        return self

    def execute(self, request: ActionRequest) -> ActionOutcome:
        self.executed.append(request)
        queue = self._plan.get(request.capability_id)
        behaviour = queue.pop(0) if queue else self.default
        if behaviour == "crash":
            raise WorldCrash(f"обрыв при выполнении {request.action_id}")
        summary = {"confirm": "сделано", "fail": "не вышло", "unknown": "ответа нет"}[behaviour]
        return ActionOutcome({"confirm": "confirmed", "fail": "failed", "unknown": "unknown"}[behaviour], summary)
