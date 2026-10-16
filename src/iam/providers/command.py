"""Сборщик команды `docker run` для изолированного исполнителя (R-110…R-115, R-149).

Каждый флаг изоляции — в одном месте и под тестом. Чего в команде **нет** намеренно: монтирований (`-v`,
`--mount`), сокета docker, `--privileged`, сетей `host`/`bridge`, общих пространств имён (`--pid`, `--ipc`),
`--env` с чем-либо, кроме перечня ниже. Единственный секрет контейнера — токен подписки, он идёт файлом
(`--env-file`, путь снаружи репозитория), а не значением в командной строке (её видно в `ps`).
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Sequence

# Переменные, которые вообще разрешено передавать контейнеру (имя → допустимый вид значения).
FIXED_ENV = {
    "HOME": "/home/claude",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
}
PROXY_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
FORBIDDEN_NETWORKS = ("host", "bridge", "none", "default")
_MEM = re.compile(r"^[1-9]\d*[mg]$")
_CPUS = re.compile(r"^\d+(\.\d+)?$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_USER = re.compile(r"^[1-9]\d{3,}:[1-9]\d{3,}$")     # только не-root, числовые uid:gid
_URL = re.compile(r"^http://[A-Za-z0-9.-]+:\d{2,5}$")

SYSTEM_PROMPT = (
    "You are the proposal engine of a digital persona core. You have no tools and you cannot act in the world. "
    "Reply with exactly one JSON object and nothing else: "
    '{"proposals": [{"kind": "...", "rationale": "...", "cites": [], "body": {...}}], "explanation": "..."}. '
    "Kinds and body fields: action{capability_id,params}; message{capability_id,to,text}; creator_request{request_key,text}; "
    "no_action{reason}; attend{focus,ref}. Use only kinds listed in allowed_proposals and only capability ids listed in "
    "allowed_actions. A proposal is a request to the core, never a report that something was done: do not claim results. "
    "Do not invent proposal ids. Ground rationale in the given context only."
)

# Схема ответа для `--json-schema`: намеренно простая (разбор и точная проверка — на нашей стороне).
RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string"}, "rationale": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "string"}}, "body": {"type": "object"}},
            "required": ["kind", "rationale", "body"]}},
        "explanation": {"type": "string"}},
    "required": ["proposals"],
}


@dataclass(frozen=True)
class SandboxConfig:
    """Параметры контейнера. Значения проверяются при создании: небезопасную конфигурацию не собрать."""

    image: str = "iam-claude:1"
    network: str = "iam-sandbox"            # внутренняя сеть без выхода наружу, кроме посредника
    proxy_url: str = "http://iam-proxy:3128"
    token_file: str | None = None            # файл с CLAUDE_CODE_OAUTH_TOKEN=… ВНЕ репозитория, права 600
    user: str = "10001:10001"
    cpus: str = "1.0"
    memory: str = "1g"
    pids: int = 128
    tmp_size: str = "64m"
    home_size: str = "128m"
    platform: str = "linux/arm64"

    def __post_init__(self):
        if not _NAME.match(self.image):
            raise ValueError(f"image {self.image!r}: недопустимое имя")
        if not _NAME.match(self.network) or self.network.lower().split(":")[0] in FORBIDDEN_NETWORKS or self.network.startswith("container"):
            raise ValueError(f"network {self.network!r}: нужна выделенная внутренняя сеть, не host/bridge/none")
        if not _URL.match(self.proxy_url):
            raise ValueError(f"proxy_url {self.proxy_url!r}: нужен http://имя:порт посредника")
        if not _USER.match(self.user):
            raise ValueError(f"user {self.user!r}: контейнер не должен работать от root (нужны числовые uid:gid ≥ 1000)")
        if not _MEM.match(self.memory) or not _CPUS.match(self.cpus) or self.pids < 1 or self.pids > 4096:
            raise ValueError("лимиты памяти/процессоров/процессов заданы неверно")
        for size in (self.tmp_size, self.home_size):
            if not _MEM.match(size):
                raise ValueError(f"размер {size!r}: нужен вид 64m")
        if self.token_file is not None:
            p = PurePosixPath(self.token_file)
            if not p.is_absolute() or ".." in p.parts:
                raise ValueError("token_file: нужен абсолютный путь вне репозитория")


@dataclass(frozen=True)
class SandboxCommand:
    argv: tuple[str, ...]
    name: str

    def kill_argv(self) -> tuple[str, ...]:
        """Остановка контейнера при таймауте: убийство клиента `docker` сам контейнер не останавливает."""
        return ("docker", "kill", self.name)


def docker_run(cfg: SandboxConfig, *, model: str = "sonnet", json_schema: dict | None = RESPONSE_JSON_SCHEMA,
               name: str | None = None, command: Sequence[str] | None = None) -> SandboxCommand:
    """Команда: контекст — в stdin, ответ — из stdout; монтирований нет, rootfs только для чтения.
    `command` заменяет `claude …` внутри тех же флагов изоляции — так `verify_isolation.sh` проверяет именно
    ту оболочку, в которой работает личность (для проверки изоляции, не для работы)."""
    if not _NAME.match(model):
        raise ValueError(f"model {model!r}: недопустимое имя модели")
    name = name or f"iam-claude-{uuid.uuid4().hex[:12]}"
    argv = ["docker", "run", "--rm", "-i", "--name", name, "--platform", cfg.platform,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", cfg.user, "--pids-limit", str(cfg.pids), "--memory", cfg.memory, "--memory-swap", cfg.memory,
            "--cpus", cfg.cpus, "--network", cfg.network,
            "--tmpfs", f"/tmp:rw,noexec,nosuid,size={cfg.tmp_size}",
            # отдельное хранилище настроек: пустой домашний каталог на каждый запуск, ничего не сохраняется
            "--tmpfs", f"/home/claude:rw,noexec,nosuid,size={cfg.home_size},uid={cfg.user.split(':')[0]},gid={cfg.user.split(':')[1]}"]
    for k, v in FIXED_ENV.items():
        argv += ["-e", f"{k}={v}"]
    for k in PROXY_ENV:
        argv += ["-e", f"{k}={cfg.proxy_url}"]
    if cfg.token_file:
        argv += ["--env-file", cfg.token_file]
    argv += [cfg.image]
    if command is not None:
        return SandboxCommand(tuple(argv + list(command)), name)
    argv += ["claude", "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
             "--permission-mode", "dontAsk", "--permission-prompts", "none", "--disable-slash-commands",
             "--strict-mcp-config", "--model", model, "--system-prompt", SYSTEM_PROMPT]
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))]
    return SandboxCommand(tuple(argv), name)
