"""Типы привратника: предложение перехода, описание события, результаты."""
from __future__ import annotations

from dataclasses import dataclass, field

from iam.domain import Event, Source, Transition

from .rules import ACTORS, event_source


@dataclass(frozen=True)
class Violation:
    """Причина отказа: `code` — машинный код, `message` — пояснение для человека."""

    code: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class EventSpec:
    """Заготовка события, которое запишет ядро вместе с переходом. Идентификатор события можно задать
    заранее (нужно, когда переход ссылается на собственное событие-основание)."""

    type: str
    idempotency_key: str
    payload: dict = field(default_factory=dict)
    causes: tuple[str, ...] = ()
    event_id: str | None = None


@dataclass(frozen=True)
class Proposal:
    """Предложение перехода — единственный вход StateController.apply.

    `transition` — что менять и на какой версии основано; `actor` — кто предлагает (operator, core,
    model, world); `carrier` — событие, которое несёт переход и его ключ идемпотентности (по умолчанию
    `state.transition_applied` с ключом `trn:<transition_id>`); `companions` — прочие события той же версии
    (например, `proposal.accepted`); `request_id` — из какого запроса к модели пришло (для записи об устаревании).
    """

    transition: Transition
    actor: str = "core"
    actor_ref: str = "state_controller"
    carrier: EventSpec | None = None
    companions: tuple[EventSpec, ...] = ()
    request_id: str | None = None

    def __post_init__(self):
        if self.actor not in ACTORS:
            raise ValueError(f"actor {self.actor!r}: нужно одно из {ACTORS}")

    def source(self) -> Source:
        return event_source(self.actor, self.actor_ref)


@dataclass(frozen=True)
class ApplyResult:
    """Итог `apply`: applied | duplicate | stale | rejected | conflict.

    `events` — записанные события (для duplicate — исходное событие-носитель); `changes` — пары (до, после)."""

    status: str
    epoch: int
    state_version: int
    reason: str | None = None
    violations: tuple[Violation, ...] = ()
    events: tuple[Event, ...] = ()
    changes: tuple[dict, ...] = ()

    @property
    def applied(self) -> bool:
        return self.status == "applied"


@dataclass(frozen=True)
class ResponseResult:
    """Итог приёма ответа модели: applied | partial | duplicate | stale | ignored | rejected | conflict.

    `accepted`/`rejected` — по `proposal_id` (rejected → причина); `transition` — результат применения."""

    status: str
    reason: str | None = None
    received: Event | None = None
    accepted: tuple[str, ...] = ()
    rejected: dict = field(default_factory=dict)
    transition: ApplyResult | None = None


@dataclass(frozen=True)
class RecordResult:
    """Итог записи события, не меняющего состояние: recorded | duplicate | rejected | conflict."""

    status: str
    event: Event | None = None
    reason: str | None = None
    violations: tuple[Violation, ...] = ()
