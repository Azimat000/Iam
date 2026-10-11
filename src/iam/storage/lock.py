"""Замок единственного писателя: одной личностью управляет один процесс (docs/spec/state.md#versioning).

`flock` на файле `<база>.lock`. Ядро ОС снимает замок само, когда процесс завершается любым способом
(в том числе `kill -9`), поэтому «зависших» замков не бывает. В файл пишется pid владельца — для диагностики.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from .errors import AlreadyLocked


class WriterLock:
    def __init__(self, db_path: str | os.PathLike):
        # resolve(): та же база через символическую ссылку или другой относительный путь — тот же замок.
        self.path = Path(str(Path(db_path).resolve()) + ".lock")
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "WriterLock":
        if self._fd is not None:
            return self
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = os.pread(fd, 32, 0).decode("ascii", "replace").strip() or "?"
            os.close(fd)
            raise AlreadyLocked(f"личностью {self.path.name[:-5]} уже управляет процесс pid={owner}") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(os.getpid()).encode("ascii"), 0)
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        os.ftruncate(fd, 0)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def __enter__(self) -> "WriterLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
