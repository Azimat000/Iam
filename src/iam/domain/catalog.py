"""Каталог типов событий (docs/spec/events.md#event-types) в машинном виде.

Для каждого типа: меняет ли состояние и шаблон ключа идемпотентности. Тест сверяет этот каталог
с таблицей в events.md и с `enum` в event.schema.json — рассинхрон ловится автоматически.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EventKind:
    type: str
    changes_state: bool
    key_template: str

    @property
    def key_re(self) -> re.Pattern:
        parts = re.split(r"(<[^>]+>)", self.key_template)
        # Значение подстановки не содержит «:», иначе ключ одного типа выдавал бы себя за ключ другого.
        rx = "".join("[^:]+" if p.startswith("<") else re.escape(p) for p in parts)
        return re.compile("^" + rx + "$")


_RAW = [
    ("persona.created", True, "per:<persona_id>:create"),
    ("input.arrived", False, "rx:<канал>:<внешний id>"),
    ("input.noticed", True, "ntc:<input_id>:<уровень>"),
    ("input.read", True, "rd:<input_id>"),
    ("input.ignored", True, "ign:<input_id>"),
    ("session.started", True, "ses:<session_id>:start"),
    ("session.paused", True, "ses:<session_id>:pause:<n>"),
    ("session.resumed", True, "ses:<session_id>:resume:<n>"),
    ("session.stopped", True, "ses:<session_id>:stop"),
    ("time.moment_due", True, "due:<moment_id>:<n>"),
    ("attention.chosen", True, "att:<cycle_id>"),
    ("activity.started", True, "act:<action_id>:start"),
    ("activity.completed", True, "act:<action_id>:done"),
    ("activity.abandoned", True, "act:<action_id>:drop"),
    ("proposal.requested", False, "req:<request_id>"),
    ("proposal.received", False, "resp:<request_id>"),
    ("proposal.assessed", False, "assess:<request_id>"),
    ("proposal.accepted", True, "prop:<proposal_id>:accept"),
    ("proposal.rejected", False, "prop:<proposal_id>:reject"),
    ("proposal.stale", False, "resp:<request_id>:stale"),
    ("action.attempted", True, "act:<action_id>:try"),
    ("action.confirmed", True, "act:<action_id>:ok"),
    ("action.failed", True, "act:<action_id>:fail"),
    ("action.unknown_outcome", True, "act:<action_id>:unk"),
    ("message.out_requested", True, "out:<action_id>"),
    ("message.out_sent", True, "out:<action_id>:ok"),
    ("message.out_unknown", True, "out:<action_id>:unk"),
    ("creator.request", True, "crq:<request_key>"),
    ("creator.reply", True, "crp:<crq_id>:<внешний id>"),
    ("state.transition_applied", True, "trn:<transition_id>"),
    ("emotion.appraised", True, "emo:<cycle_id>:<event_id>"),
    ("belief.updated", True, "bel:<transition_id>"),
    ("intention.changed", True, "int:<transition_id>"),
    ("reflection.done", True, "ref:<cycle_id>"),
    ("memory.recorded", True, "mem:<transition_id>:<n>"),
    ("memory.linked", True, "lnk:<transition_id>:<n>"),
    ("memory.retrieved", False, "rtv:<cycle_id>:<n>"),
    ("memory.generalized", True, "gen:<transition_id>"),
    ("memory.revised", True, "rev:<transition_id>"),
    ("memory.faded", True, "fad:<transition_id>"),
    ("operator.intervention", True, "opr:<intervention_id>"),
    ("system.limit_reached", True, "lim:<session_id>:<вид>"),
    ("system.error", False, "err:<сбой>:<n>"),
    ("system.rejected_sender", False, "rjs:<канал>:<внешний id>"),
    ("system.backup_created", False, "bak:<snapshot_id>"),
    ("system.restored", True, "rst:<snapshot_id>"),
]

CATALOG: dict[str, EventKind] = {t: EventKind(t, c, k) for t, c, k in _RAW}
EVENT_TYPES: tuple[str, ...] = tuple(CATALOG)


def check_event_kind(type_: str, changes_state: bool, idempotency_key: str) -> list[str]:
    """Согласованность события с каталогом: известный тип, флаг состояния, формат ключа."""
    kind = CATALOG.get(type_)
    if kind is None:
        return [f"неизвестный тип события {type_!r}"]
    errs = []
    if changes_state != kind.changes_state:
        errs.append(f"{type_}: changes_state должно быть {kind.changes_state}")
    if not kind.key_re.match(idempotency_key):
        errs.append(f"{type_}: ключ {idempotency_key!r} не соответствует шаблону {kind.key_template!r}")
    return errs
