"""Классификатор результата запуска (R-109, R-117, R-118): что вернул исполнитель и можно ли этому верить.

Формат `claude -p --output-format json` сверен с документацией (code.claude.com, headless) и с реальным образцом
ошибки авторизации, снятым в среде разработки: объект с `is_error`, `result`, `api_error_status`,
`permission_denials`, при успехе с `structured_output`. Код возврата процесса классификатору не доверяем: образец
ошибки авторизации вышел с кодом 0. Образцы успеха и лимита подписки снимает живая проверка на Mac (G11).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    OK = "ok"
    AUTH = "auth"
    LIMIT = "limit"
    UNAVAILABLE = "unavailable"
    FORMAT = "format"
    TIMEOUT = "timeout"
    TOOL_USE = "tool_use"
    EXEC = "exec"          # исполнитель не запустился (нет docker, образ, сеть контейнера)
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Classified:
    kind: Kind
    message: str = ""
    data: dict | None = None            # разобранный результат модели (для OK и TOOL_USE)
    envelope: dict = field(default_factory=dict)   # внешний объект claude (без текста результата)


_AUTH_STATUS = {401, 403}
_LIMIT_STATUS = {429}
_AUTH_WORDS = re.compile(r"(?i)authenticat|oauth|unauthori|invalid (api )?key|login|expired|account|organization|billing")
_LIMIT_WORDS = re.compile(r"(?i)usage limit|rate.?limit|limit reached|too many requests|quota")
_DOWN_WORDS = re.compile(r"(?i)overloaded|unavailable|internal server|timed? ?out|connection|network|bad gateway")
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def _meta(env: dict) -> dict:
    """Служебные поля без текста результата (текст может содержать лишнее; в журнал идёт только метаданные)."""
    keep = ("is_error", "api_error_status", "terminal_reason", "subtype", "num_turns", "stop_reason", "session_id",
            "total_cost_usd", "duration_ms", "permission_denials")
    return {k: env[k] for k in keep if k in env}


def _error_kind(env: dict) -> Kind:
    status, text = env.get("api_error_status"), str(env.get("result", ""))
    if status in _AUTH_STATUS:
        return Kind.AUTH
    if status in _LIMIT_STATUS:
        return Kind.LIMIT
    if isinstance(status, int) and status >= 500:
        return Kind.UNAVAILABLE
    # статуса нет (или он ничего не говорит): разбираем текст; лимит проверяем раньше входа («account … limit»)
    if _LIMIT_WORDS.search(text):
        return Kind.LIMIT
    if _AUTH_WORDS.search(text):
        return Kind.AUTH
    if _DOWN_WORDS.search(text):
        return Kind.UNAVAILABLE
    return Kind.UNKNOWN


def _model_payload(env: dict) -> dict | None:
    """Результат модели: `structured_output` (если был `--json-schema`) или JSON в тексте `result`."""
    structured = env.get("structured_output")
    if isinstance(structured, dict):
        return structured
    text = env.get("result")
    if not isinstance(text, str):
        return None
    m = _FENCE.match(text)
    try:
        value = json.loads(m.group(1) if m else text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def classify(run) -> Classified:
    """`run` — `RunResult`. Порядок важен: таймаут → не JSON → ошибка API → попытка инструментов → формат."""
    if run.timed_out:
        return Classified(Kind.TIMEOUT, "исполнитель не ответил вовремя")
    try:
        env = json.loads(run.stdout)
    except ValueError:
        env = None
    if not isinstance(env, dict):
        if run.returncode not in (0, None):
            return Classified(Kind.EXEC, f"исполнитель не запустился или упал (код {run.returncode}): {run.stderr.strip()[:300]}")
        return Classified(Kind.FORMAT, "вывод исполнителя не JSON-объект")
    meta = _meta(env)
    if env.get("is_error"):
        return Classified(_error_kind(env), str(env.get("result", ""))[:300], envelope=meta)
    data = _model_payload(env)
    if env.get("permission_denials"):   # модель пыталась пользоваться инструментом: результат непригоден (R-117)
        return Classified(Kind.TOOL_USE, "модель пыталась использовать инструмент", data, meta)
    if data is None:
        return Classified(Kind.FORMAT, "в результате нет JSON-объекта", None, meta)
    return Classified(Kind.OK, "", data, meta)
