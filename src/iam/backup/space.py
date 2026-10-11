"""Защита от нехватки места (R-205): запас проверяется до записи, при нехватке сеанс останавливается чисто.

Принцип: данные портит не «мало места», а запись, оборвавшаяся посередине. Поэтому (1) перед записью требуется
запас — размер того, что будем писать, плюс постоянный резерв `reserve` (из него оплачивается запись события
остановки); (2) если места нет, контроллер записывает ошибку и останавливает сеанс, пока ещё может; (3) если
SQLite всё же сообщил «диск полон», транзакция откатилась целиком (атомарность S04), и сеанс останавливается.
"""
from __future__ import annotations

import errno
import secrets
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from iam.state import EventSpec

from .errors import NotEnoughSpace

T = TypeVar("T")

DEFAULT_RESERVE = 64 * 1024 * 1024   # 64 МиБ: хватает на WAL-запись события остановки с большим запасом


def is_disk_full(exc: BaseException) -> bool:
    """Ошибка именно «нет места»: ENOSPC/EDQUOT или SQLITE_FULL."""
    if isinstance(exc, NotEnoughSpace):
        return True
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return True
    if isinstance(exc, sqlite3.Error):
        return getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL or "disk is full" in str(exc).lower()
    return False


class SpaceGuard:
    """Проверка свободного места рядом с `path` (каталог базы или копий). `usage` подменяется в тестах."""

    def __init__(self, path: str | Path, *, reserve: int = DEFAULT_RESERVE,
                 usage: Callable[[str], tuple[int, int, int]] = shutil.disk_usage):
        if reserve < 0:
            raise ValueError("резерв не может быть отрицательным")
        self.path = Path(path)
        self.reserve = reserve
        self._usage = usage

    def _existing(self) -> Path:
        p = self.path
        while not p.exists() and p != p.parent:
            p = p.parent
        return p

    def free(self) -> int:
        return self._usage(str(self._existing()))[2]

    def ensure(self, need: int = 0) -> int:
        """Убедиться, что свободно не меньше `need` + резерв; иначе `NotEnoughSpace`. Возвращает свободное."""
        free = self.free()
        if free < need + self.reserve:
            raise NotEnoughSpace(free, need + self.reserve, str(self.path))
        return free


@dataclass(frozen=True)
class StopOutcome:
    """Что удалось сделать при нехватке места: записать ошибку и остановить сеанс."""

    error_recorded: bool
    session_stopped: bool


def stop_on_low_space(controller, error: BaseException) -> StopOutcome:
    """Чистая остановка: записать `system.error` (fault `disk_full`) и остановить сеанс. Каждый шаг сам может
    упереться в нехватку места — тогда он откатывается целиком (база цела) и мы идём дальше; любая другая
    ошибка — настоящая, пробрасывается."""
    recorded = stopped = False
    try:
        res = controller.record_event(EventSpec("system.error", f"err:disk_full:{secrets.token_hex(8)}",
                                                {"fault": "disk_full", "message": str(error)[:300]}))
        recorded = res.status == "recorded"
    except Exception as e:  # noqa: BLE001 — пробрасываем всё, кроме «нет места»
        if not is_disk_full(e):
            raise
    if controller.session_state in ("running", "paused"):
        try:
            stopped = controller.stop_session().status == "applied"
        except Exception as e:  # noqa: BLE001
            if not is_disk_full(e):
                raise
    return StopOutcome(recorded, stopped)


def guarded_call(controller, guard: SpaceGuard, fn: Callable[[], T], *, need: int = 0) -> T:
    """Выполнить запись под защитой места: запас проверяется заранее; «диск полон» во время записи тоже
    приводит к остановке сеанса. Исключение пробрасывается в любом случае — нельзя объявлять выполненным то,
    что не записано."""
    try:
        guard.ensure(need)
    except NotEnoughSpace as e:
        stop_on_low_space(controller, e)
        raise
    try:
        return fn()
    except BaseException as e:
        if is_disk_full(e):
            stop_on_low_space(controller, e)
        raise
