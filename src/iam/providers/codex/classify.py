"""Fail-closed classification of Codex exec JSONL. Raw diagnostics never escape."""
from __future__ import annotations

import json
import re

from ..classify import Classified, Kind
from ..run import RunResult


def _error_kind(event: dict) -> Kind:
    text = json.dumps(event).lower()
    if re.search(r'429|rate.?limit|usage.?limit|quota|too many requests', text):
        return Kind.LIMIT
    if re.search(r'401|403|authenticat|unauthori|oauth|expired|sign.?in|login|api.?key', text):
        return Kind.AUTH
    return Kind.UNAVAILABLE


def _json(text: str):
    # Python otherwise accepts NaN/Infinity and duplicate properties, unlike JSON Schema.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)


def classify(run: RunResult) -> Classified:
    events, malformed, tool = [], False, False
    for line in run.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = _json(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError
        except (ValueError, RecursionError):
            malformed = True
            continue
        events.append(event)
        if event.get("type", "").startswith("item."):
            item = event.get("item")
            # New/unknown item types are capabilities until explicitly audited as passive.
            if isinstance(item, dict) and item.get("type") not in ("agent_message", "reasoning"):
                tool = True
        if re.search(r'tool|command|mcp|web|file_change', str(event.get("type", "")), re.I):
            tool = True
    # An attempted tool is never hidden by a later failure, timeout, or malformed output.
    if tool:
        return Classified(Kind.TOOL_USE, "Codex attempted a tool")
    if run.timed_out:
        return Classified(Kind.TIMEOUT, "Codex exceeded its deadline")
    for event in events:
        if event.get("type") in ("error", "turn.failed", "turn.interrupted"):
            return Classified(_error_kind(event), "Codex reported a failed turn")
    if run.returncode != 0 or run.stderr.strip():
        return Classified(_error_kind({"message": run.stderr}), "Codex process failed")
    if malformed:
        return Classified(Kind.FORMAT, "Codex emitted malformed JSONL")
    stage, messages, pending, completed = 0, [], set(), set()
    for event in events:
        kind = event.get("type")
        if kind == "thread.started" and stage == 0 and isinstance(event.get("thread_id"), str) and event["thread_id"]:
            stage = 1
        elif kind == "turn.started" and stage == 1:
            stage = 2
        elif kind in ("item.started", "item.updated", "item.completed") and stage == 2:
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") not in ("agent_message", "reasoning"):
                return Classified(Kind.UNKNOWN, "Codex emitted an invalid item")
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id or item_id in completed:
                return Classified(Kind.UNKNOWN, "Codex item identity is invalid")
            if item.get("status") not in ((None, "completed") if kind == "item.completed" else (None, "in_progress")) or item.get("error"):
                return Classified(Kind.UNAVAILABLE, "Codex item did not complete cleanly")
            if kind == "item.completed":
                pending.discard(item_id)
                completed.add(item_id)
            else:
                pending.add(item_id)
            if item["type"] == "agent_message" and kind == "item.completed":
                if not isinstance(item.get("text"), str) or item.get("phase") not in (None, "final_answer"):
                    return Classified(Kind.FORMAT, "Codex final message is invalid")
                messages.append(item["text"])
        elif kind == "turn.completed" and stage == 2:
            if pending or event.get("error") or event.get("status") not in (None, "completed"):
                return Classified(Kind.UNAVAILABLE, "Codex turn did not complete cleanly")
            stage = 3
        else:
            return Classified(Kind.UNKNOWN, "Codex emitted an unexpected event sequence")
    if stage != 3:
        return Classified(Kind.UNAVAILABLE, "Codex stream ended before clean completion")
    if len(messages) != 1:
        return Classified(Kind.FORMAT, "Codex must emit exactly one final message")
    try:
        data = _json(messages[0])
    except (ValueError, RecursionError):
        return Classified(Kind.FORMAT, "Codex final message is not JSON")
    if not isinstance(data, dict):
        return Classified(Kind.FORMAT, "Codex final message must be an object")
    return Classified(Kind.OK, data=data)
