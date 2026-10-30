"""Subscription-only Codex implementation of the shared CognitiveProvider protocol."""
from __future__ import annotations

import json
import math
import time

from iam.domain import jsonschema, new_id

from ..classify import Classified, Kind
from ..errors import (ProviderAuthError, ProviderFormatError, ProviderLimitError, ProviderTimeout, ProviderUnavailable)
from ..run import Runner, subprocess_runner
from .auth import read_auth
from .classify import _json, classify
from .command import DEFAULT_MODEL, REPO_ROOT, SYSTEM_PROMPT, CodexSandboxConfig, docker_run


def build_prompt(request: dict) -> str:
    view = {"purpose": request["purpose"],
            "context": [{k: item[k] for k in ("ref", "origin", "layer", "text")} for item in request["context"]],
            "allowed_proposals": request["allowed_proposals"], "allowed_actions": request["allowed_actions"],
            "max_output_tokens": request["limits"]["max_output_tokens"]}
    return json.dumps(view, ensure_ascii=False, indent=1)


def _proposals(data: dict) -> list[dict]:
    if set(data) != {"proposals", "explanation"} or not isinstance(data["explanation"], str):
        raise ValueError
    if not isinstance(data["proposals"], list):
        raise ValueError
    proposals = []
    for item in data["proposals"]:
        if not isinstance(item, dict) or set(item) != {"kind", "rationale", "cites", "body_json"}:
            raise ValueError
        if not isinstance(item["body_json"], str):
            raise ValueError
        body = _json(item["body_json"])
        if not isinstance(body, dict):
            raise ValueError
        proposal = {k: item[k] for k in ("kind", "rationale", "cites")}
        proposal.update(proposal_id=new_id("prop"), body=body)
        if jsonschema.errors("proposal", proposal):
            raise ValueError
        proposals.append(proposal)
    return proposals


_ERRORS = {Kind.AUTH: ProviderAuthError, Kind.LIMIT: ProviderLimitError, Kind.TIMEOUT: ProviderTimeout}


class CodexProvider:
    name = "codex"

    def __init__(self, runner: Runner, sandbox: CodexSandboxConfig, *, model: str = DEFAULT_MODEL,
                 timeout_s: float = 180.0, max_format_retries: int = 1):
        if not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Codex timeout must be positive and finite")
        if type(max_format_retries) is not int or max_format_retries not in (0, 1):
            raise ValueError("Codex permits at most one format retry")
        docker_run(sandbox, model=model)  # Validate model before any runner call.
        self._run, self.sandbox, self.model = runner, sandbox, model
        self.timeout_s, self.max_format_retries = timeout_s, max_format_retries
        self.last_calls = 0

    def propose(self, request: dict) -> dict:
        started, self.last_calls = time.monotonic(), 0
        timeout = min(self.timeout_s, request["limits"]["deadline_ms"] / 1000)
        deadline = started + timeout
        prompt = SYSTEM_PROMPT + "\n\n" + build_prompt(request)
        if self.sandbox.auth_file is None and self._run is subprocess_runner:
            raise ProviderAuthError("Codex requires subscription credentials")
        for attempt in range(self.max_format_retries + 1):
            auth = read_auth(self.sandbox.auth_file, REPO_ROOT)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderTimeout("Codex exceeded its total deadline")
            command = docker_run(self.sandbox, model=self.model)
            actual_prompt = prompt if attempt == 0 else prompt + (
                "\n\nYour previous reply was rejected. Reply again with exactly one JSON object matching the schema.")
            payload = json.dumps({"auth": auth, "prompt": actual_prompt}, ensure_ascii=False)
            run = self._run(command.argv, payload, remaining, command.kill_argv())
            self.last_calls += 1
            result = classify(run)
            if result.kind is Kind.TOOL_USE:
                return self._response(request, [], result, started, True)
            if time.monotonic() > deadline:
                raise ProviderTimeout("Codex exceeded its total deadline")
            if result.kind is Kind.OK:
                try:
                    proposals = _proposals(result.data)
                except (ValueError, RecursionError):
                    continue
                return self._response(request, proposals, result, started, False)
            if result.kind is not Kind.FORMAT:
                raise _ERRORS.get(result.kind, ProviderUnavailable)(result.message)
        raise ProviderFormatError("Codex response did not match the required format")

    def _response(self, request: dict, proposals: list[dict], result: Classified, started: float, tool_use: bool) -> dict:
        return {"request_id": request["request_id"], "epoch": request["epoch"], "state_version": request["state_version"],
                "provider": {"name": self.name, "model": self.model}, "status": "ok", "proposals": proposals,
                "explanation": "" if tool_use else result.data["explanation"],
                "usage": {"calls": self.last_calls, "duration_ms": max(0, int((time.monotonic() - started) * 1000))},
                "tool_use_detected": tool_use}
