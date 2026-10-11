"""Проверка окружения при запуске (R-147): Python ≥ 3.13 и SQLite с FTS5."""
from __future__ import annotations

import sqlite3
import sys

MIN_PYTHON = (3, 13)


def has_fts5(connect=sqlite3.connect) -> bool:
    con = connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def runtime_problems(version_info=None, connect=sqlite3.connect) -> list[str]:
    """Список несоответствий окружения (пустой — всё в порядке)."""
    v = tuple(version_info or sys.version_info)[:2]
    probs = []
    if v < MIN_PYTHON:
        probs.append(f"нужен Python ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]}, а запущен {v[0]}.{v[1]}")
    if not has_fts5(connect):
        probs.append(f"SQLite {sqlite3.sqlite_version} собран без FTS5 (полнотекстовый поиск)")
    return probs


def check_runtime(version_info=None, connect=sqlite3.connect) -> None:
    from .errors import RuntimeUnsupported

    probs = runtime_problems(version_info, connect)
    if probs:
        raise RuntimeUnsupported("; ".join(probs))
