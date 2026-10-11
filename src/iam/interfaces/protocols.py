"""Шесть внутренних интерфейсов ядра (PLAN §2.8, R-106). Это «розетки»: цикл знает только их.

Заглушки (`iam.stubs`), настоящие исполнители (S08, S09, S18+) и тест-двойники подключаются без правок цикла.
Проверка соответствия — `isinstance(x, Protocol)` (проверяет наличие членов) плюс тесты поведения.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from iam.domain import Event, MemoryRecord
from iam.state import ApplyResult, KindHandler, Proposal, RecordResult, ResponseResult

from .types import (ActionOutcome, ActionRequest, Capability, CreatorRequest, Delivery, InboundMessage)


@runtime_checkable
class CognitiveProvider(Protocol):
    """Получить структурированное предложение от выбранного механизма (Codex, Claude, правила…).

    `propose` получает запрос (`request.schema.json`) и возвращает ответ (`response.schema.json`) — только
    предложения: ни действий, ни записей. Не смог ответить — `ProviderError`."""

    name: str

    def propose(self, request: dict) -> dict: ...


@runtime_checkable
class Memory(Protocol):
    """Извлекать воспоминания и решать, что запомнить. Писать в хранилище память не может: запись идёт
    только переходом через StateController (R-105)."""

    def retrieve(self, query: str, *, limit: int = 5) -> list[MemoryRecord]: ...

    def describe(self, action: ActionRequest, outcome: ActionOutcome) -> str | None:
        """Текст воспоминания о подтверждённом исходе или None («запоминать нечего»)."""
        ...


@runtime_checkable
class World(Protocol):
    """Выполнить действие в собственной среде и честно сообщить исход. Обрыв — исключение (исход неизвестен)."""

    def execute(self, request: ActionRequest) -> ActionOutcome: ...


@runtime_checkable
class Capabilities(Protocol):
    """Реестр возможностей: что доступно, что временно не работает, что требует разрешения оператора."""

    def list(self) -> list[Capability]: ...

    def get(self, capability_id: str) -> Capability | None: ...


@runtime_checkable
class CreatorBridge(Protocol):
    """Передать просьбу оператору и принять его сообщения."""

    def submit(self, request: CreatorRequest) -> Delivery: ...

    def poll_replies(self) -> list[InboundMessage]: ...


@runtime_checkable
class StateController(Protocol):
    """То, что цикл берёт у проверяющего ядра. Настоящая реализация — `iam.state.StateController`."""

    persona_id: str
    clock: object

    @property
    def epoch(self) -> int: ...

    @property
    def state_version(self) -> int: ...

    @property
    def state(self) -> dict: ...

    @property
    def session_state(self) -> str: ...

    @property
    def session_id(self) -> str | None: ...

    def apply(self, proposal: Proposal) -> ApplyResult: ...

    def record_event(self, spec, *, actor: str = "core", actor_ref: str = "state_controller") -> RecordResult: ...

    def record_request(self, request: dict) -> RecordResult: ...

    def receive_response(self, response: dict) -> ResponseResult: ...

    def register_kind(self, kind: str, handler: KindHandler) -> None: ...

    def find_event(self, *, event_id: str | None = None, key: str | None = None) -> Event | None: ...

    def events_after(self, seq: int) -> list[Event]: ...

    def tick(self) -> list[ApplyResult]: ...
