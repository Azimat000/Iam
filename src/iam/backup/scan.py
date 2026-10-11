"""Сканер секретов (R-204, R-219): токены Telegram и провайдеров, ключи, пароли и «канареечные» секреты.

Два режима: `find` — только найти (для проверки уже готового пакета и репозитория), `redact` — заменить найденное
меткой (экспорт редактирует копию данных, оригинал не трогает). Значения секретов никогда не попадают в
результаты: наружу идут вид находки, место и маска.

«Канарейка» — заведомо фальшивый секрет, подложенный тестом в данные; точное вхождение (в сыром, JSON-экранированном
виде и в base64) всегда находка. Секреты окружения (`IAM_*TOKEN`, `*_SECRET`, `*_PASSWORD`…) подхватываются
автоматически: значение, попавшее в данные личности, не должно уйти в экспорт.
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

REDACTED = "[СЕКРЕТ УДАЛЁН]"

# Шаблоны высокой точности: формат секрета узнаваем сам по себе.
STRICT_PATTERNS: dict[str, re.Pattern] = {
    "telegram_bot_token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "provider_api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer_token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
}
# Присваивание «слово-секрет = значение»: шире, поэтому только для данных личности (в репозитории есть примеры в документах).
ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|passwd|auth(?:orization)?)[\w-]*\s*[:=]\s*[\"']?"
    r"(?P<value>(?=[^\s\"']*\d)(?=[^\s\"']*[A-Za-z])[^\s\"',;]{12,})")
ENV_SECRET_NAME = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|AUTH)")
MIN_ENV_SECRET = 8


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int

    def mask(self, text: str) -> str:
        raw = text[self.start:self.end]
        return f"{raw[:2]}…({len(raw)} зн.)" if len(raw) > 6 else "…"


def _canary_forms(secret: str) -> set[str]:
    raw = secret.encode()
    forms = {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1],
             base64.b64encode(raw).decode().rstrip("="), base64.urlsafe_b64encode(raw).decode().rstrip("=")}
    return {f for f in forms if f}


def env_secrets(environ: Mapping[str, str] | None = None) -> list[str]:
    """Значения секретных переменных окружения (по имени), достаточно длинные, чтобы не ловить случайное."""
    env = os.environ if environ is None else environ
    return sorted({v for k, v in env.items() if ENV_SECRET_NAME.search(k) and len(v) >= MIN_ENV_SECRET})


class SecretScanner:
    def __init__(self, canaries: Iterable[str] = (), *, use_env: bool = True,
                 environ: Mapping[str, str] | None = None, assignments: bool = True):
        secrets_ = {c for c in canaries if c}
        if any(len(c) < 4 for c in secrets_):
            raise ValueError("канареечный секрет короче 4 символов даст ложные срабатывания")
        if use_env:
            secrets_ |= set(env_secrets(environ))
        self.known = tuple(sorted(secrets_))
        self._forms = {s: _canary_forms(s) for s in self.known}
        self.assignments = assignments

    def find(self, text: str) -> list[Finding]:
        """Все находки в тексте, без пересечений, по порядку."""
        raw: list[Finding] = []
        for kind, pat in STRICT_PATTERNS.items():
            raw += [Finding(kind, m.start(), m.end()) for m in pat.finditer(text)]
        if self.assignments:
            raw += [Finding("secret_assignment", m.start("value"), m.end("value")) for m in ASSIGNMENT.finditer(text)]
        for forms in self._forms.values():
            for form in forms:
                start = text.find(form)
                while start != -1:
                    raw.append(Finding("known_secret", start, start + len(form)))
                    start = text.find(form, start + 1)
        raw.sort(key=lambda f: (f.start, -f.end))
        merged: list[Finding] = []
        for f in raw:
            if merged and f.start < merged[-1].end:
                last = merged[-1]
                if f.end > last.end:
                    merged[-1] = Finding(last.kind, last.start, f.end)
                continue
            merged.append(f)
        return merged

    def redact(self, text: str) -> tuple[str, list[Finding]]:
        found = self.find(text)
        out, pos = [], 0
        for f in found:
            out += [text[pos:f.start], REDACTED]
            pos = f.end
        out.append(text[pos:])
        return "".join(out), found

    def redact_value(self, value: Any, counts: dict[str, int] | None = None) -> Any:
        """Рекурсивно редактирует строки (и ключи) в структуре JSON; `counts` пополняется по видам находок."""
        if isinstance(value, str):
            text, found = self.redact(value)
            for f in found:
                if counts is not None:
                    counts[f.kind] = counts.get(f.kind, 0) + 1
            return text
        if isinstance(value, list):
            return [self.redact_value(v, counts) for v in value]
        if isinstance(value, dict):
            return {self.redact_value(k, counts): self.redact_value(v, counts) for k, v in value.items()}
        return value

    def describe(self, text: str, where: str) -> list[str]:
        """Находки в тексте в виде строк «где: вид, маска» (для сообщений об ошибке)."""
        return [f"{where}: {f.kind} {f.mask(text)}" for f in self.find(text)]
