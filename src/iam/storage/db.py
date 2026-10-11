"""Соединение с SQLite и транзакции.

- WAL + `synchronous=FULL`: зафиксированная транзакция переживает аварию процесса и питания.
- Транзакции — только явные (`BEGIN IMMEDIATE`): изменение записывается целиком или не записывается.
- Сторож (authorizer) на наших соединениях вне миграций запрещает DROP/ALTER, смену опасных PRAGMA
  (`writable_schema`, `ignore_check_constraints`, `foreign_keys`, режим журнала и др., в любом регистре)
  и прямую запись в индекс поиска, чтобы код ядра не мог случайно снести защиту журнала. От человека с прямым доступом к файлу
  это не защищает — защита файла от оператора вне задачи (копии и проверка целостности — S06).
"""
from __future__ import annotations

import contextlib
import itertools
import sqlite3
from pathlib import Path

_DENY_ALWAYS = {sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_INDEX,
                sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_VTABLE, sqlite3.SQLITE_ALTER_TABLE,
                sqlite3.SQLITE_DROP_TEMP_TABLE, sqlite3.SQLITE_DROP_TEMP_TRIGGER}


# PRAGMA, которые нельзя *менять* после открытия: ослабили бы защиту схемы, ограничения или надёжность.
_LOCKED_PRAGMAS = {"writable_schema", "ignore_check_constraints", "foreign_keys", "journal_mode", "synchronous",
                   "trusted_schema", "recursive_triggers"}
_WRITES = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_TRIGGER_ONLY_TABLES = {"memory_fts"}  # индекс поиска пишется только триггерами memory_records


class Guard:
    """Authorizer: вне режима миграции запрещает изменение схемы, опасные PRAGMA и прямую запись в индекс."""

    def __init__(self):
        self.migrating = False

    def __call__(self, action, arg1, arg2, dbname, source):
        if self.migrating:
            return sqlite3.SQLITE_OK
        if action in _DENY_ALWAYS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and arg1.lower() in _LOCKED_PRAGMAS and arg2 is not None:
            return sqlite3.SQLITE_DENY
        if action in _WRITES and arg1 in _TRIGGER_ONLY_TABLES and source is None:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextlib.contextmanager
    def unlocked(self):
        """Временно снять сторожа (миграции, служебные команды FTS)."""
        prev, self.migrating = self.migrating, True
        try:
            yield
        finally:
            self.migrating = prev


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
    con.execute("PRAGMA trusted_schema = OFF")
    if not readonly:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = FULL")
    con.guard = Guard()
    con.set_authorizer(con.guard)
    return con


_savepoints = itertools.count(1)


@contextlib.contextmanager
def transaction(con: sqlite3.Connection):
    """Атомарный блок. Внешний блок — `BEGIN IMMEDIATE … COMMIT`; вложенный — точка сохранения
    (SAVEPOINT): исключение во вложенном блоке откатывает только его записи, а если исключение долетело
    до внешнего блока — откатывается всё. Сбой при фиксации откатывает транзакцию и не оставляет
    соединение «висеть» в ней."""
    if con.in_transaction:
        name = f"iam_sp_{next(_savepoints)}"
        con.execute(f"SAVEPOINT {name}")
        try:
            yield con
        except BaseException:
            if con.in_transaction:
                con.execute(f"ROLLBACK TO {name}")
                con.execute(f"RELEASE {name}")
            raise
        con.execute(f"RELEASE {name}")
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:  # SQLite мог уже откатить сам (например, при нехватке места)
            con.execute("ROLLBACK")
        raise
