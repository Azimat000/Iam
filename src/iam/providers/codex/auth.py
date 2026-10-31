"""Read subscription auth securely; never return a path, token, or raw error in diagnostics."""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path

from ..errors import ProviderAuthError

TOKEN_FIELDS = ('access_token', 'id_token', 'refresh_token', 'account_id')


def normalize_auth(raw: dict, *, include_refresh: bool = False) -> dict:
    """Known subscription fields only; CLI bookkeeping never becomes configuration."""
    if not isinstance(raw, dict) or raw.get('auth_mode') != 'chatgpt' or raw.get('OPENAI_API_KEY') or set(raw) - {'auth_mode', 'tokens', 'OPENAI_API_KEY', 'last_refresh'}:
        raise ValueError
    tokens = raw.get('tokens')
    if not isinstance(tokens, dict) or set(tokens) - {*TOKEN_FIELDS, 'login_owner_id', 'oauth_provenance'}:
        raise ValueError
    if any(not isinstance(tokens.get(k), str) or not tokens[k].strip()
           for k in ('access_token', 'id_token', 'refresh_token')):
        raise ValueError
    if 'account_id' in tokens and (not isinstance(tokens['account_id'], str) or not tokens['account_id'].strip()):
        raise ValueError
    result = {'auth_mode': 'chatgpt', 'tokens': {k: tokens[k] for k in TOKEN_FIELDS if k in tokens}}
    if include_refresh and 'last_refresh' in raw:
        if not isinstance(raw['last_refresh'], str) or datetime.fromisoformat(raw['last_refresh']).tzinfo is None:
            raise ValueError
        result['last_refresh'] = raw['last_refresh']
    return result


def validate_auth_path(value: str, repo: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.resolve().is_relative_to(repo):
        raise ValueError("auth_file must be an absolute path outside the repository")
    try:
        info = path.lstat()
    except OSError:
        raise ValueError("auth_file is unavailable") from None
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("auth_file must be a regular non-symlink file with mode 600")
    return path


def read_auth(value: str | None, repo: Path) -> dict:
    if value is None:
        return {}  # Only fake runners may operate without subscription credentials.
    try:
        path = validate_auth_path(value, repo)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536:
                raise ValueError
            raw = json.load(stream)
        return normalize_auth(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ProviderAuthError("Codex subscription credentials are unavailable or invalid") from None
