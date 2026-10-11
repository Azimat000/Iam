"""Разборщики видов предложений для привратника (`StateController.register_kind`).

Каждый превращает **предложение** в операции состояния «принято, ещё не сделано» (или отклоняет его).
Само действие цикл выполняет позже, после проверки ядром, и только его подтверждённый исход становится
воспоминанием (envelopes.md#confirmation; R-199, R-222).
"""
from __future__ import annotations

import re

from iam.domain import Op
from iam.interfaces import Capabilities
from iam.state import KindContext, KindDecision, KindHandler, Violation

from .validator import body_violation

REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
EXECUTABLE = ("action", "message")


def action_id_for(proposal_id: str) -> str:
    """Идентификатор действия выводится из предложения: повтор разбора даёт тот же `act_…`."""
    return "act_" + proposal_id.removeprefix("prop_")


def _set(group: str, path: str, value) -> Op:
    return Op(group, path, "set", value, True)


def _no(code: str, message: str) -> KindDecision:
    return KindDecision(violation=Violation(code, message))


def make_kind_handlers(capabilities: Capabilities, *, max_actions: int = 1) -> dict[str, KindHandler]:
    """Разборщики для `action`, `message`, `creator_request`, `no_action`, `attend`.
    `max_actions` — сколько действий/сообщений можно принять из одного ответа (предел решений)."""

    def checked(pr: dict) -> KindDecision | None:
        bad = body_violation(pr["kind"], pr["body"])
        return _no("unknown_body_field", bad) if bad else None

    def executable(pr: dict, ctx: KindContext) -> KindDecision:
        if (early := checked(pr)) is not None:
            return early
        body, cap_id = pr["body"], pr["body"]["capability_id"]
        cap = capabilities.get(cap_id)
        if cap is None or not cap.available:
            return _no("capability_not_available",
                       f"возможности {cap_id} нет в реестре или она не доступна ({cap.status if cap else 'нет'})")
        if cap_id not in ctx.request["allowed_actions"]:
            return _no("capability_not_allowed", f"{cap_id} не входит в allowed_actions запроса")
        if sum(k in EXECUTABLE for k in ctx.accepted_kinds) >= max_actions:
            return _no("too_many_actions", f"из одного ответа принимается не более {max_actions} действий")
        record = {"status": "accepted", "kind": pr["kind"], "capability_id": cap_id,
                  "params": body.get("params", {}), "proposal_id": pr["proposal_id"],
                  "cycle_id": ctx.request["cycle_id"]}
        if pr["kind"] == "message":
            record |= {"to": body["to"], "text": body["text"]}
        return KindDecision((_set("world_time", f"world.actions.{action_id_for(pr['proposal_id'])}", record),))

    def creator_request(pr: dict, ctx: KindContext) -> KindDecision:
        if (early := checked(pr)) is not None:
            return early
        key = pr["body"]["request_key"]
        if not REQUEST_KEY_RE.match(key):
            return _no("bad_request_key", "request_key: 1–40 символов из букв, цифр, «_» и «-»")
        if key in ctx.state["world_time"].get("world", {}).get("creator_requests", {}):
            return _no("duplicate_request", f"просьба {key} уже отправлялась: без новых обстоятельств не повторяется")
        return KindDecision((_set("world_time", f"world.creator_requests.{key}",
                                  {"status": "accepted", "text": pr["body"]["text"],
                                   "proposal_id": pr["proposal_id"]}),))

    def no_action(pr: dict, ctx: KindContext) -> KindDecision:
        if (early := checked(pr)) is not None:
            return early
        return KindDecision((_set("affect", "attention", {
            "focus": "rest", "ref": None, "reason": pr["body"]["reason"], "since_life_ms": ctx.life_time_ms}),))

    def attend(pr: dict, ctx: KindContext) -> KindDecision:
        if (early := checked(pr)) is not None:
            return early
        return KindDecision((_set("affect", "attention", {
            "focus": pr["body"]["focus"], "ref": pr["body"].get("ref"), "since_life_ms": ctx.life_time_ms}),))

    return {"action": executable, "message": executable, "creator_request": creator_request,
            "no_action": no_action, "attend": attend}
