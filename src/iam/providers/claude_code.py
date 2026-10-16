"""ClaudeCodeProvider — адаптер `CognitiveProvider` к подписке Claude Code (`claude -p` в изолированном контейнере).

Контекст уходит в stdin, ответ читается из stdout (R-116). Модель получает только необходимые фрагменты (R-208, G09):
вид задачи, элементы контекста (ссылка, происхождение, слой, текст), допустимые виды предложений и возможности —
без идентификаторов личности и сеанса, без служебных ключей. Идентификаторы предложений назначает адаптер, эхо
запроса, `provider`, `usage` и `tool_use_detected` заполняет он же: модель их подделать не может. Результат
проверяется классификатором (R-109) и схемой предложения; одна повторная попытка при неверном формате (R-194).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from iam.domain import jsonschema, new_id

from .classify import Classified, Kind, classify
from .command import RESPONSE_JSON_SCHEMA, SandboxConfig, docker_run
from .errors import (ProviderAuthError, ProviderFormatError, ProviderLimitError, ProviderTimeout, ProviderUnavailable)
from .run import Runner

PURPOSES = ("appraise", "choose_action", "compose_message", "reflect", "summarize", "generalize")


@dataclass(frozen=True)
class ModelRouting:
    """Маршрутизация по классу задачи (R-192, R-193): простое — дешёвой моделью, заранее перечисленные сложные
    операции — сильной. Имена — псевдонимы из `claude --help` (sonnet, opus); меняются настройкой."""

    cheap: str = "sonnet"
    strong: str = "opus"
    strong_purposes: frozenset[str] = field(default_factory=lambda: frozenset({"reflect", "generalize"}))

    def __post_init__(self):
        unknown = set(self.strong_purposes) - set(PURPOSES)
        if unknown:
            raise ValueError(f"неизвестные классы задач: {sorted(unknown)}")

    def model_for(self, purpose: str) -> str:
        return self.strong if purpose in self.strong_purposes else self.cheap


def build_prompt(request: dict) -> str:
    """Единственное, что видит модель (кроме системной инструкции). Ничего лишнего: ни `persona_id`, ни `session_id`,
    ни ключей идемпотентности, ни времени."""
    view = {"purpose": request["purpose"],
            "context": [{"ref": c["ref"], "origin": c["origin"], "layer": c["layer"], "text": c["text"]}
                        for c in request["context"]],
            "allowed_proposals": request["allowed_proposals"], "allowed_actions": request["allowed_actions"],
            "max_output_tokens": request["limits"]["max_output_tokens"]}
    return json.dumps(view, ensure_ascii=False, indent=1)


def _repair_prompt(prompt: str, problem: str) -> str:
    return (f"{prompt}\n\nYour previous reply was rejected: {problem[:300]}\n"
            "Reply again with exactly one valid JSON object of the required shape and nothing else.")


def _with_ids(data: dict) -> tuple[list[dict], str | None]:
    """Предложения из ответа модели: назначить наши идентификаторы и проверить форму. (список, текст ошибки)."""
    raw = data.get("proposals")
    if not isinstance(raw, list):
        return [], "поле proposals должно быть списком"
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"proposals[{i}] должно быть объектом"
        pr = {"proposal_id": new_id("prop"), "cites": [], **{k: v for k, v in item.items() if k != "proposal_id"}}
        errs = jsonschema.errors("proposal", pr)
        if errs:
            return [], f"proposals[{i}]: {errs[0]}"
        out.append(pr)
    return out, None


class ClaudeCodeProvider:
    name = "claude-code"

    def __init__(self, runner: Runner, sandbox: SandboxConfig, *, routing: ModelRouting | None = None,
                 timeout_s: float = 180.0, use_json_schema: bool = True, max_format_retries: int = 1):
        self._run, self.sandbox = runner, sandbox
        self.routing = routing or ModelRouting()
        self.timeout_s, self.use_json_schema = timeout_s, use_json_schema
        self.max_format_retries = max_format_retries
        self.last_calls = 0     # сколько запусков исполнителя потребовало последнее обращение

    def propose(self, request: dict) -> dict:
        model = self.routing.model_for(request["purpose"])
        prompt = build_prompt(request)
        started, self.last_calls, problem = time.monotonic(), 0, ""
        for attempt in range(self.max_format_retries + 1):
            cmd = docker_run(self.sandbox, model=model, json_schema=RESPONSE_JSON_SCHEMA if self.use_json_schema else None)
            run = self._run(cmd.argv, prompt if attempt == 0 else _repair_prompt(prompt, problem), self.timeout_s,
                            cmd.kill_argv())
            self.last_calls += 1
            result = classify(run)
            if result.kind is Kind.OK:
                proposals, problem = _with_ids(result.data)
                if problem is None:
                    return self._response(request, model, proposals, result, started, tool_use=False)
            elif result.kind is Kind.TOOL_USE:
                proposals, _ = _with_ids(result.data or {})
                return self._response(request, model, proposals, result, started, tool_use=True)
            elif result.kind is Kind.FORMAT:
                problem = result.message
            else:
                raise _ERRORS.get(result.kind, ProviderUnavailable)(f"{result.kind}: {result.message}")
        raise ProviderFormatError(f"неверный формат после {self.max_format_retries + 1} попыток: {problem}")

    def _response(self, request: dict, model: str, proposals: list[dict], result: Classified, started: float,
                  *, tool_use: bool) -> dict:
        explanation = (result.data or {}).get("explanation")
        return {"request_id": request["request_id"], "epoch": request["epoch"], "state_version": request["state_version"],
                "provider": {"name": self.name, "model": model}, "status": "ok", "proposals": proposals,
                "explanation": explanation if isinstance(explanation, str) else "",
                "usage": {"calls": self.last_calls, "duration_ms": int((time.monotonic() - started) * 1000)},
                "tool_use_detected": tool_use}


_ERRORS = {Kind.AUTH: ProviderAuthError, Kind.LIMIT: ProviderLimitError, Kind.UNAVAILABLE: ProviderUnavailable,
           Kind.TIMEOUT: ProviderTimeout, Kind.EXEC: ProviderUnavailable, Kind.UNKNOWN: ProviderUnavailable}
