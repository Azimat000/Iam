"""Соединение с SQLite и транзакции.

- WAL + `synchronous=FULL`: зафиксированная транзакция переживает аварию процесса и питания.
- Транзакции — только явные (`BEGIN IMMEDIATE`): изменение записывается целиком или не записывается.
- Сторож (authorizer) на наших соединениях запрещает DROP/ALTER и `writable_schema` вне миграций,
  чтобы код ядра не мог случайно снести триггеры журнала. От человека с прямым доступом к файлу
  это не защищает — защита файла от оператора вне задачи (копии и проверка целостности — S06).
"""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

_DENY_ALWAYS = {sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_INDEX,
                sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_VTABLE, sqlite3.SQLITE_ALTER_TABLE,
                sqlite3.SQLITE_DROP_TEMP_TABLE, sqlite3.SQLITE_DROP_TEMP_TRIGGER}


class Guard:
    """Authorizer: вне режима миграции запрещает изменение схемы и опасные PRAGMA."""

    def __init__(self):
        self.migrating = False

    def __call__(self, action, arg1, arg2, dbname, source):
        if self.migrating:
            return sqlite3.SQLITE_OK
        if action in _DENY_ALWAYS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "writable_schema" and arg2:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


class Connection(sqlite3.Connection):
    """Соединение ядра: несёт своего сторожа (`guard`)."""

    guard: Guard


def connect(path: str | Path, *, readonly: bool = False) -> Connection:
    """Открыть базу с настройками ядра. Транзакциями управляет только `transaction()`."""
    if readonly:
        con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, isolation_level=None,
                              factory=Connection)
    else:
        con = sqlite3.connect(str(path), isolation_level=None, factory=Connection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 5000")
    con.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = FULL")
    con.guard = Guard()
    con.set_authorizer(con.guard)
    return con


@contextlib.contextmanager
def transaction(con: sqlite3.Connection):
    """Атомарный блок. Вложенный вызов входит во внешнюю транзакцию: исключение откатывает всё,
    фиксирует только самый внешний блок."""
    owner = not con.in_transaction
    if owner:
        con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        if owner:
            con.execute("ROLLBACK")
        raise
    if owner:
        con.execute("COMMIT")
