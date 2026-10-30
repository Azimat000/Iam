"""Deterministic Codex JSONL fixtures; these never call Docker or a model."""
from __future__ import annotations

import json
from collections import deque
from typing import Iterable, Sequence

from ..run import RunResult


def events(*items: dict, returncode: int = 0, stderr: str = "") -> RunResult:
    return RunResult(returncode, "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n", stderr)


def reply_text(text: str) -> RunResult:
    return events({"type": "thread.started", "thread_id": "fake-thread"}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}},
                  {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})


def reply(proposals: list[dict], explanation: str = "fake reply") -> RunResult:
    wire = [{**{k: v for k, v in proposal.items() if k != "body"},
             "cites": proposal.get("cites", []), "body_json": json.dumps(proposal["body"], ensure_ascii=False)} for proposal in proposals]
    return reply_text(json.dumps({"proposals": wire, "explanation": explanation}, ensure_ascii=False))


def garbage(text: str = "not JSON") -> RunResult:
    return reply_text(text)


def error(status: int | None, text: str) -> RunResult:
    return events({"type": "turn.failed", "error": {"status": status, "message": text}})


def auth_error() -> RunResult:
    return error(401, "Authentication failed")


def rate_limited() -> RunResult:
    return error(429, "Usage limit reached")


def overloaded() -> RunResult:
    return error(503, "Service unavailable")


def tool_attempt(proposals: list[dict] | None = None) -> RunResult:
    good = reply(proposals or [])
    lines = good.stdout.splitlines()
    lines.insert(2, json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}}))
    return RunResult(0, "\n".join(lines))


def hang() -> RunResult:
    return RunResult(None, timed_out=True)


def docker_failed() -> RunResult:
    return RunResult(125, stderr="Docker unavailable")


class FakeCodex:
    def __init__(self, results: Iterable[RunResult] = ()):
        self._results = deque(results)
        self.calls: list[dict] = []

    def queue(self, *results: RunResult) -> FakeCodex:
        self._results.extend(results)
        return self

    def __call__(self, argv: Sequence[str], stdin: str, timeout_s: float,
                 cleanup: Sequence[str] | None = None) -> RunResult:
        self.calls.append({"argv": list(argv), "stdin": stdin, "timeout_s": timeout_s, "cleanup": list(cleanup or [])})
        if not self._results:
            raise AssertionError("FakeCodex: no canned results remain")
        return self._results.popleft()
