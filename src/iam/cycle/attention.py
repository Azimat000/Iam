"""Внимание в каркасе: простое правило «куда смотреть» по событию-поводу (настоящее внимание — S20).

Содержимое непрочитанного сообщения сюда не попадает: для `input.arrived` берётся только идентификатор
входящего (P-1, R-038).
"""
from __future__ import annotations

from dataclasses import dataclass

from iam.domain import Event


@dataclass(frozen=True)
class Focus:
    focus: str            # activity | intention | event | dialogue | rest (state.md#affect)
    ref: str | None = None
    query: str = ""       # чем запрашивать память


class AttentionPolicy:
    def choose(self, trigger: Event, state: dict) -> Focus:
        if trigger.type == "input.arrived":
            return Focus("event", trigger.payload.get("input_id"))
        if trigger.type == "time.moment_due":
            ref = trigger.payload.get("owner_ref")
            intention = state["intentions"].get(ref) if ref else None
            text = intention.get("text", "") if isinstance(intention, dict) else ""
            return Focus("intention" if text else "activity", ref, text)
        return Focus("activity")
