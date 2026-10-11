"""Типы домена из спецификации S02: событие, запись памяти, переход состояния.

Каждый тип: `to_dict()` (ровно форма схемы), `from_dict()` (с проверкой по JSON-схеме),
`validate()` (схема + правила, которые схема выразить не может, например каталог событий).
Канонический JSON (`canonical_json`) — для хранения и сравнения при идемпотентности.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from . import jsonschema
from .catalog import check_event_kind

STATE_GROUPS = ("traits", "affect", "beliefs", "relations", "intentions", "preferences", "selfmodel", "world_time")
MEMORY_LAYERS = ("archive", "personal", "interpretation")
MEMORY_KINDS = ("sensory", "working", "episodic", "semantic", "autobiographical", "emotional_associative",
                "procedural", "social", "prospective")
ORIGINS = ("observation", "model_knowledge", "stated_by_other", "own_hypothesis", "imagination",
           "simulation_event", "model_explanation")


def canonical_json(value: Any) -> str:
    """Однозначная запись JSON: ключи по порядку, без лишних пробелов, кириллица как есть."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Source:
    kind: str
    ref: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref}

    @classmethod
    def from_dict(cls, d: dict) -> "Source":
        return cls(kind=d["kind"], ref=d["ref"])


@dataclass(frozen=True)
class Event:
    """Событие журнала (docs/spec/events.md#event-envelope)."""

    event_id: str
    persona_id: str
    type: str
    occurred_at: str
    life_time_ms: int
    source: Source
    idempotency_key: str
    epoch: int
    state_version: int
    changes_state: bool
    payload: dict = field(default_factory=dict)
    causes: tuple[str, ...] = ()
    seq: int | None = None
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "persona_id": self.persona_id,
            "seq": self.seq,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "life_time_ms": self.life_time_ms,
            "source": self.source.to_dict(),
            "causes": list(self.causes),
            "idempotency_key": self.idempotency_key,
            "epoch": self.epoch,
            "state_version": self.state_version,
            "changes_state": self.changes_state,
            "payload": self.payload,
            "schema_version": self.schema_version,
        }

    def errors(self) -> list[str]:
        errs = jsonschema.errors("event", self.to_dict())
        if errs:
            return errs
        return check_event_kind(self.type, self.changes_state, self.idempotency_key)

    def validate(self) -> "Event":
        errs = self.errors()
        if errs:
            raise jsonschema.ValidationError("event", errs)
        return self

    def with_seq(self, seq: int) -> "Event":
        return replace(self, seq=seq)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        jsonschema.validate("event", d)
        ev = cls(
            event_id=d["event_id"], persona_id=d["persona_id"], type=d["type"], occurred_at=d["occurred_at"],
            life_time_ms=d["life_time_ms"], source=Source.from_dict(d["source"]),
            idempotency_key=d["idempotency_key"], epoch=d["epoch"], state_version=d["state_version"],
            changes_state=d["changes_state"], payload=d["payload"], causes=tuple(d["causes"]), seq=d["seq"],
            schema_version=d["schema_version"],
        )
        return ev.validate()


@dataclass(frozen=True)
class Link:
    type: str
    target: str

    def to_dict(self) -> dict:
        return {"type": self.type, "target": self.target}


_MEMORY_OPTIONAL = ("claim_kind", "availability", "detail", "valence", "arousal", "restored_by_operator",
                    "independence_group")


@dataclass(frozen=True)
class MemoryRecord:
    """Запись памяти — одна модель для всех слоёв и видов (docs/spec/provenance.md)."""

    memory_id: str
    persona_id: str
    layer: str
    kind: str
    origin: str
    content: dict
    source: Source
    created_at: str
    created_life_ms: int
    sources: tuple[str, ...]
    links: tuple[Link, ...] = ()
    history: tuple[dict, ...] = ()
    version: int = 1
    claim_kind: str | None = None
    availability: float | None = None
    detail: float | None = None
    valence: float | None = None
    arousal: float | None = None
    restored_by_operator: bool | None = None
    independence_group: str | None = None

    @property
    def text(self) -> str:
        return self.content["text"]

    def to_dict(self) -> dict:
        d = {
            "memory_id": self.memory_id,
            "persona_id": self.persona_id,
            "layer": self.layer,
            "kind": self.kind,
            "origin": self.origin,
            "content": self.content,
            "source": self.source.to_dict(),
            "created_at": self.created_at,
            "created_life_ms": self.created_life_ms,
            "sources": list(self.sources),
            "links": [x.to_dict() for x in self.links],
            "history": [dict(h) for h in self.history],
            "version": self.version,
        }
        for k in _MEMORY_OPTIONAL:
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

    def errors(self) -> list[str]:
        return jsonschema.errors("memory_record", self.to_dict())

    def validate(self) -> "MemoryRecord":
        jsonschema.validate("memory_record", self.to_dict())
        return self

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryRecord":
        jsonschema.validate("memory_record", d)
        return cls(
            memory_id=d["memory_id"], persona_id=d["persona_id"], layer=d["layer"], kind=d["kind"],
            origin=d["origin"], content=d["content"], source=Source.from_dict(d["source"]),
            created_at=d["created_at"], created_life_ms=d["created_life_ms"], sources=tuple(d["sources"]),
            links=tuple(Link(x["type"], x["target"]) for x in d["links"]),
            history=tuple(dict(h) for h in d["history"]), version=d["version"],
            **{k: d[k] for k in _MEMORY_OPTIONAL if k in d},
        )


@dataclass(frozen=True)
class Op:
    group: str
    path: str
    op: str
    value: Any = None
    has_value: bool = False

    def to_dict(self) -> dict:
        d = {"group": self.group, "path": self.path, "op": self.op}
        if self.has_value:
            d["value"] = self.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Op":
        return cls(d["group"], d["path"], d["op"], d.get("value"), "value" in d)


@dataclass(frozen=True)
class Transition:
    """Переход состояния (docs/spec/state.md#versioning); применяет только StateController (S05)."""

    transition_id: str
    persona_id: str
    base_epoch: int
    base_version: int
    ops: tuple[Op, ...]
    reason: str
    caused_by: tuple[str, ...]
    proposal_id: str | None = None

    def to_dict(self) -> dict:
        d = {
            "transition_id": self.transition_id,
            "persona_id": self.persona_id,
            "base_epoch": self.base_epoch,
            "base_version": self.base_version,
            "ops": [o.to_dict() for o in self.ops],
            "reason": self.reason,
            "caused_by": list(self.caused_by),
        }
        if self.proposal_id is not None:
            d["proposal_id"] = self.proposal_id
        return d

    def validate(self) -> "Transition":
        jsonschema.validate("transition", self.to_dict())
        return self

    @classmethod
    def from_dict(cls, d: dict) -> "Transition":
        jsonschema.validate("transition", d)
        return cls(
            transition_id=d["transition_id"], persona_id=d["persona_id"], base_epoch=d["base_epoch"],
            base_version=d["base_version"], ops=tuple(Op.from_dict(o) for o in d["ops"]),
            reason=d["reason"], caused_by=tuple(d["caused_by"]), proposal_id=d.get("proposal_id"),
        )


def validate_envelope(name: str, value: dict) -> dict:
    """Конверты обмена с моделью (request/response/proposal) — пока словари с проверкой по схеме;
    типизированные классы появятся вместе с интерфейсами (S07)."""
    if name not in ("request", "response", "proposal"):
        raise KeyError(f"{name!r} — не конверт обмена с моделью")
    jsonschema.validate(name, value)
    return value
