"""ScriptedProvider — «интеллект» без интеллекта: отдаёт заранее записанные ответы по очереди.

Нужен, чтобы пройти весь цикл без настоящей модели (допущение A2) и показать, что провайдер заменяем.
Заглушка ничего не выполняет: она лишь формирует предложения в формате `proposal.schema.json`.
"""
from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable

from iam.domain import new_id
from iam.interfaces import ProviderError


class ScriptExhausted(ProviderError):
    """Записанные ответы кончились, а цикл снова обратился к провайдеру."""


@dataclass
class Reply:
    """Один ответ заглушки. `before` вызывается до ответа (например, чтобы изменить состояние «пока модель
    думала» и сделать ответ устаревшим); `error` — вместо ответа бросить исключение; `raw` — отдать этот словарь
    как есть (для проверки неверного формата)."""

    proposals: tuple[dict, ...] = ()
    status: str = "ok"
    explanation: str = "ответ заглушки"
    tool_use: bool = False
    before: Callable[[dict], None] | None = None
    error: Exception | None = None
    raw: dict | None = None
    extra: dict = field(default_factory=dict)


def _proposal(kind: str, body: dict, rationale: str, cites: Iterable[str]) -> dict:
    return {"proposal_id": new_id("prop"), "kind": kind, "rationale": rationale, "cites": list(cites), "body": body}


def act(capability_id: str, params: dict | None = None, *, rationale: str = "нужно сделать дело", cites=(),
        **body) -> dict:
    return _proposal("action", {"capability_id": capability_id, "params": params or {}, **body}, rationale, cites)


def say(capability_id: str, to: str, text: str, *, rationale: str = "хочу написать", cites=(), **body) -> dict:
    return _proposal("message", {"capability_id": capability_id, "to": to, "text": text, **body}, rationale, cites)


def rest(reason: str = "ничего не нужно", *, rationale: str = "отдохну", cites=()) -> dict:
    return _proposal("no_action", {"reason": reason}, rationale, cites)


def attend(focus: str, ref: str | None = None, *, rationale: str = "обращу внимание", cites=()) -> dict:
    return _proposal("attend", {"focus": focus, **({"ref": ref} if ref else {})}, rationale, cites)


def ask(request_key: str, text: str, *, rationale: str = "нужна помощь оператора", cites=()) -> dict:
    return _proposal("creator_request", {"request_key": request_key, "text": text}, rationale, cites)


def change(ops: list[dict], *, rationale: str = "изменю состояние", cites=()) -> dict:
    return _proposal("state_change", {"ops": ops}, rationale, cites)


class ScriptedProvider:
    """Реализация `CognitiveProvider`: `calls` хранит все полученные запросы (для проверок)."""

    def __init__(self, replies: Iterable[Reply | dict | Callable[[dict], Reply]] = (), *, name: str = "scripted",
                 model: str = "scripted-1"):
        self.name = name
        self.model = model
        self.calls: list[dict] = []
        self._replies = deque(replies)

    def queue(self, *replies: Reply | dict | Callable[[dict], Reply]) -> "ScriptedProvider":
        self._replies.extend(replies)
        return self

    def propose(self, request: dict) -> dict:
        self.calls.append(copy.deepcopy(request))
        if not self._replies:
            raise ScriptExhausted("заглушке нечем отвечать: сценарий закончился")
        reply = self._replies.popleft()
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, dict):
            return reply
        if reply.before is not None:
            reply.before(request)
        if reply.error is not None:
            raise reply.error
        if reply.raw is not None:
            return reply.raw
        return {"request_id": request["request_id"], "epoch": request["epoch"],
                "state_version": request["state_version"], "provider": {"name": self.name, "model": self.model},
                "status": reply.status, "proposals": [copy.deepcopy(p) for p in reply.proposals],
                "explanation": reply.explanation, "usage": {"calls": 1, "duration_ms": 1},
                "tool_use_detected": reply.tool_use, **reply.extra}
