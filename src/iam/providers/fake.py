"""FakeClaude — подделка исполнителя для тестов: отдаёт заготовленные результаты запуска вместо docker и `claude`.

Форма результатов повторяет `claude -p --output-format json`. Образец ошибки авторизации снят реально
(токен-пустышка, среда разработки); остальные ошибки собраны по документации и перепроверяются на Mac (G11).
"""
from __future__ import annotations

import json
from collections import deque
from typing import Iterable, Sequence

from .run import RunResult

_ENVELOPE = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "permission_denials": [],
             "stop_reason": "end_turn", "terminal_reason": "completed", "total_cost_usd": 0, "duration_ms": 1200}


def _out(**fields) -> RunResult:
    return RunResult(0, json.dumps({**_ENVELOPE, **fields}, ensure_ascii=False))


def reply(proposals: list[dict], explanation: str = "ответ подделки") -> RunResult:
    """Успешный структурированный ответ (`structured_output`, как при `--json-schema`)."""
    return _out(result="", structured_output={"proposals": proposals, "explanation": explanation})


def reply_text(text: str) -> RunResult:
    """Успех без `--json-schema`: JSON лежит в тексте `result` (возможно, в блоке ```json```)."""
    return _out(result=text)


def garbage(text: str = "Конечно! Вот что я думаю…") -> RunResult:
    return _out(result=text)


def tool_attempt(proposals: list[dict] | None = None) -> RunResult:
    """Модель пыталась вызвать инструмент: отказ записан в `permission_denials`."""
    return _out(result="", structured_output={"proposals": proposals or [], "explanation": ""},
                permission_denials=[{"tool_name": "Bash", "tool_use_id": "toolu_fake", "tool_input": {"command": "ls"}}])


def error(status: int | None, text: str, *, terminal: str = "api_error") -> RunResult:
    return _out(is_error=True, api_error_status=status, result=text, terminal_reason=terminal)


def auth_error() -> RunResult:
    """Реальный образец: токен недействителен (HTTP 401), код возврата процесса — 0."""
    return error(401, "Failed to authenticate. API Error: 401 OAuth access token is invalid.")


def rate_limited() -> RunResult:
    return error(429, "Claude usage limit reached. Your limit will reset at 5pm.")


def overloaded() -> RunResult:
    return error(529, "API Error: 529 Overloaded")


def hang() -> RunResult:
    return RunResult(None, "", "", True)


def docker_failed() -> RunResult:
    return RunResult(125, "", "docker: Cannot connect to the Docker daemon. Is the docker daemon running?")


class FakeClaude:
    """Реализует `Runner`: по очереди отдаёт результаты и запоминает всё, что ему передали (`calls`)."""

    def __init__(self, results: Iterable[RunResult] = ()):
        self._results = deque(results)
        self.calls: list[dict] = []

    def queue(self, *results: RunResult) -> "FakeClaude":
        self._results.extend(results)
        return self

    def __call__(self, argv: Sequence[str], stdin: str, timeout_s: float, cleanup: Sequence[str] | None = None) -> RunResult:
        self.calls.append({"argv": list(argv), "stdin": stdin, "timeout_s": timeout_s, "cleanup": list(cleanup or [])})
        if not self._results:
            raise AssertionError("FakeClaude: заготовленные результаты кончились")
        return self._results.popleft()
