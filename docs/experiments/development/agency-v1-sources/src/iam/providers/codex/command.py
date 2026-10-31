"""Bounded Docker command; credentials and model context travel only over stdin."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .auth import validate_auth_path
from .auth_session import validate_writeback_path

DEFAULT_MODEL = "gpt-6-luna"
REPO_ROOT = Path(__file__).resolve().parents[4]
SYSTEM_PROMPT = (
    "You are the proposal engine of a digital persona core. You have no tools and cannot act in the world. "
    "Reply with exactly one JSON object containing proposals and explanation. "
    "Each proposal has kind, rationale, cites (context references), and body_json, a JSON-encoded object string. "
    "Kinds and body fields: action{capability_id,params}; message{capability_id,to,text}; "
    "creator_request{request_key,text}; no_action{reason}; attend{focus,ref}; state_change{ops}; memory_write{draft}; intention{op}. "
    "Use only allowed_proposals and allowed_actions. Proposals request actions from the core; never claim results. "
    "Do not invent proposal ids. Ground rationale only in the supplied context."
)
RESPONSE_JSON_SCHEMA = {
    "type": "object", "properties": {
        "proposals": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["action", "message", "state_change", "memory_write", "intention", "creator_request", "no_action", "attend"]}, "rationale": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "string"}}, "body_json": {"type": "string"}},
            "required": ["kind", "rationale", "cites", "body_json"], "additionalProperties": False}},
        "explanation": {"type": "string"}},
    "required": ["proposals", "explanation"], "additionalProperties": False,
}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_MEMORY = re.compile(r"([1-9]\d*)([mg])")


def _size(value: str, maximum_mb: int) -> bool:
    match = _MEMORY.fullmatch(value)
    return bool(match and int(match[1]) * (1024 if match[2] == "g" else 1) <= maximum_mb)


@dataclass(frozen=True)
class CodexSandboxConfig:
    auth_file: str | None = None
    image: str = "iam-codex:1"
    network: str = "iam-sandbox"
    proxy_url: str = "http://iam-proxy:3128"
    user: str = "10001:10001"
    cpus: str = "1.0"
    memory: str = "1g"
    pids: int = 128
    tmp_size: str = "64m"
    home_size: str = "128m"
    work_size: str = "64m"
    platform: str = "linux/arm64"
    persist_auth_updates: bool = False

    def __post_init__(self):
        if not _NAME.fullmatch(self.image):
            raise ValueError("invalid Codex image")
        if not re.fullmatch(r"iam-sandbox[A-Za-z0-9_.-]*", self.network):
            raise ValueError("Codex requires a dedicated internal iam-sandbox network")
        if not re.fullmatch(r"http://[A-Za-z0-9.-]+:[0-9]{2,5}", self.proxy_url):
            raise ValueError("invalid Codex proxy URL")
        if not 1 <= int(self.proxy_url.rsplit(":", 1)[1]) <= 65535:
            raise ValueError("invalid proxy port")
        if self.user != "10001:10001" or self.platform != "linux/arm64":
            raise ValueError("Codex requires uid/gid 10001 and linux/arm64")
        if not re.fullmatch(r"\d+(\.\d+)?", self.cpus) or not 0 < float(self.cpus) <= 4:
            raise ValueError("Codex CPU bound must be between 0 and 4")
        if not _size(self.memory, 4096) or type(self.pids) is not int or not 1 <= self.pids <= 512:
            raise ValueError("invalid Codex memory or process bound")
        if not all(_size(s, 512) for s in (self.tmp_size, self.home_size, self.work_size)):
            raise ValueError("invalid Codex tmpfs bound")
        if self.auth_file is not None:
            validate_auth_path(self.auth_file, REPO_ROOT)
        if type(self.persist_auth_updates) is not bool:
            raise ValueError('persist_auth_updates must be a boolean')
        if self.persist_auth_updates:
            validate_writeback_path(self.auth_file, REPO_ROOT)


@dataclass(frozen=True)
class SandboxCommand:
    argv: tuple[str, ...]
    name: str

    def kill_argv(self) -> tuple[str, ...]:
        return ("docker", "kill", self.name)


def docker_run(cfg: CodexSandboxConfig, *, model: str = DEFAULT_MODEL,
               name: str | None = None, command: Sequence[str] | None = None) -> SandboxCommand:
    if not _NAME.fullmatch(model):
        raise ValueError("invalid Codex model")
    name = name or f"iam-codex-{uuid.uuid4().hex[:12]}"
    if not re.fullmatch(r"iam-codex-[A-Za-z0-9]{1,32}", name):
        raise ValueError("invalid Codex container name")
    argv = ["docker", "run", "--rm", "-i", "--pull", "never", "--name", name, "--platform", cfg.platform,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", cfg.user, "--pids-limit", str(cfg.pids), "--memory", cfg.memory, "--memory-swap", cfg.memory,
            "--cpus", cfg.cpus, "--network", cfg.network, "--workdir", "/work"]
    for path, size in (("/tmp", cfg.tmp_size), ("/home/codex", cfg.home_size), ("/work", cfg.work_size)):
        argv += ["--tmpfs", f"{path}:rw,noexec,nosuid,nodev,size={size},uid=10001,gid=10001,mode=0700"]
    for key, value in {"HOME": "/home/codex", "CODEX_HOME": "/home/codex/.codex",
                       "HTTP_PROXY": cfg.proxy_url, "HTTPS_PROXY": cfg.proxy_url,
                       "WS_PROXY": cfg.proxy_url, "WSS_PROXY": cfg.proxy_url,
                       "http_proxy": cfg.proxy_url, "https_proxy": cfg.proxy_url}.items():
        argv += ["-e", f"{key}={value}"]
    if cfg.persist_auth_updates and command is None:
        argv += ['-e', 'IAM_AUTH_WRITEBACK=1']
    argv += [cfg.image]
    tail = list(command) if command is not None else [
        "node", "/opt/iam/launch.cjs", "codex", "exec", "--model", model, "--json", "--ephemeral",
        "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
        "--sandbox", "read-only", "-c", "features.hooks=false", "-c", "features.shell_tool=false",
        "-c", "features.multi_agent=false",
        "--output-schema", "/opt/iam/response.schema.json", "-"]
    return SandboxCommand(tuple(argv + tail), name)
