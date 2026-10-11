"""Типы обмена между циклом и «розетками» (интерфейсами): возможность, действие, исход, просьба, входящее.

Всё неизменяемое и проверяется при создании: неверный статус — ошибка сразу, а не молчаливый сбой в цикле.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CAPABILITY_STATUSES = ("available", "unavailable_temporarily", "needs_permission", "revoked")
ACTION_KINDS = ("action", "message")
OUTCOME_STATUSES = ("confirmed", "failed", "unknown")


class ProviderError(Exception):
    """Интеллектуальный механизм не смог ответить (сеть, лимит, авторизация…)."""


@dataclass(frozen=True)
class Capability:
    """Запись реестра возможностей (permissions.md#registry). Права меняет только оператор."""

    capability_id: str
    name: str
    description: str = ""
    status: str = "available"
    limits: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in CAPABILITY_STATUSES:
            raise ValueError(f"status {self.status!r}: нужно одно из {CAPABILITY_STATUSES}")

    @property
    def available(self) -> bool:
        return self.status == "available"


@dataclass(frozen=True)
class ActionRequest:
    """Что просят выполнить миру: действие или исходящее сообщение через возможность."""

    action_id: str
    kind: str
    capability_id: str
    params: dict = field(default_factory=dict)
    to: str | None = None
    text: str | None = None

    def __post_init__(self):
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"kind {self.kind!r}: нужно одно из {ACTION_KINDS}")


@dataclass(frozen=True)
class ActionOutcome:
    """Исход, о котором сообщает мир. `confirmed` — мир подтвердил; `failed` — мир подтвердил неудачу;
    `unknown` — подтверждения нет (потерянный ответ, обрыв): это не успех и не провал."""

    status: str
    summary: str = ""

    def __post_init__(self):
        if self.status not in OUTCOME_STATUSES:
            raise ValueError(f"status {self.status!r}: нужно одно из {OUTCOME_STATUSES}")


@dataclass(frozen=True)
class CreatorRequest:
    """Просьба личности к оператору (`creator.request`)."""

    crq_id: str
    request_key: str
    text: str


@dataclass(frozen=True)
class Delivery:
    delivered: bool
    detail: str = ""


@dataclass(frozen=True)
class InboundMessage:
    """Входящее от оператора. Пока цикл не «прочитал» его (стадия `read`, S20), содержимое в запросы не попадает."""

    channel: str
    external_id: str
    sender: str
    text: str
