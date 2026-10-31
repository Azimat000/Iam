"""Single writer for an explicitly selected private auth file; no raw diagnostics escape."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..errors import ProviderAuthError, ProviderTimeout
from ..run import RunResult
from .auth import normalize_auth, validate_auth_path
from .classify import _json

AUTH_EVENT = 'iam.auth.updated'
_FAILURE = 'Codex private credential handoff failed; session must stop'


def validate_writeback_path(value: str | None, repo: Path) -> Path:
    if value is None:
        raise ValueError('private auth file is required for writeback')
    path = validate_auth_path(value, repo).resolve()
    # Native login always writes auth.json. IAM owns a separately named import.
    if path.name == 'auth.json':
        raise ValueError('writeback requires a separate IAM-managed file')
    info, parent = path.stat(), path.parent.stat()
    if info.st_nlink != 1 or info.st_uid != os.getuid() or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise ValueError('private auth file and directory must be owned and protected')
    return path


def _read(path: Path, repo: Path) -> bytes:
    validate_writeback_path(str(path), repo)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError
    return data


class AuthSession:
    def __init__(self, path: Path, repo: Path):
        self.path, self.repo = path, repo
        self.original = _read(path, repo)
        self.auth = normalize_auth(_json(self.original.decode('utf-8')), include_refresh=True)

    def accept(self, run: RunResult) -> RunResult:
        if run.timed_out:
            raise ProviderTimeout('Codex timed out before private credential handoff')
        try:
            lines = run.stdout.splitlines()
            while lines and not lines[-1].strip():
                lines.pop()
            if not lines or len(lines[-1].encode('utf-8')) > 65536:
                raise ValueError
            event = _json(lines[-1])
            if not isinstance(event, dict) or set(event) != {'type', 'auth'} or event['type'] != AUTH_EVENT:
                raise ValueError
            updated = normalize_auth(event['auth'], include_refresh=True)
            if updated != self.auth:
                self._save(updated)
            # The private event never reaches the model response parser or journal.
            return RunResult(run.returncode, '\n'.join(lines[:-1]) + '\n', run.stderr, run.timed_out)
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise ProviderAuthError(_FAILURE) from None

    def _save(self, updated: dict) -> None:
        if hashlib.sha256(_read(self.path, self.repo)).digest() != hashlib.sha256(self.original).digest():
            raise ValueError
        data = (json.dumps(updated, ensure_ascii=False) + '\n').encode('utf-8')
        fd, name = tempfile.mkstemp(prefix='.iam-auth-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Detect outside changes visible before commit. Only IAM may write this file;
            # arbitrary writers ignoring the sidecar lock cannot be coordinated here.
            if _read(self.path, self.repo) != self.original:
                raise ValueError
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.original, self.auth = data, updated
        finally:
            Path(name).unlink(missing_ok=True)


@contextmanager
def auth_session(value: str, repo: Path):
    fd = None
    try:
        try:
            path = validate_writeback_path(value, repo)
            fd = os.open(path.with_name(path.name + '.iam-lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            session = AuthSession(path, repo)
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise ProviderAuthError(_FAILURE) from None
        yield session
    finally:
        if fd is not None:
            os.close(fd)
