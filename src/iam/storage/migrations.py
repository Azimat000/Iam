"""Схема базы личности и мигратор с таблицей версий.

v0 — пустой файл (таблиц нет). Каждая миграция — список SQL-операторов; применяется в одной
транзакции вместе с записью в `schema_version`, поэтому прерванная миграция не оставляет полсхемы.
Повторный запуск безопасен: уже применённые версии пропускаются. Файл с чужими таблицами и без
`schema_version` не трогаем (`UnknownDatabase`). Перед миграцией непустой базы вызывается хук
`before` — в него S06 встроит резервную копию (R-200).
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Callable

from .db import transaction
from .errors import SchemaTooNew, UnknownDatabase


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


def _append_only(table: str, *, deny_delete: bool = True, when: str = "") -> list[str]:
    """Триггеры «только дописывать»: UPDATE (и DELETE) отклоняются самой базой."""
    cond = f" WHEN {when}" if when else ""
    out = [
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}{cond} "
        f"BEGIN SELECT RAISE(ABORT, 'iam: {table} только дописывается (UPDATE запрещён)'); END",
    ]
    if deny_delete:
        out.append(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}{cond} "
            f"BEGIN SELECT RAISE(ABORT, 'iam: {table} только дописывается (DELETE запрещён)'); END")
    return out


def _no_replace(table: str, key_cond: str) -> str:
    """INSERT OR REPLACE удалил бы старую строку в обход триггеров DELETE — запрещаем любую вставку
    поверх существующего ключа."""
    return (f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {key_cond}) "
            f"BEGIN SELECT RAISE(ABORT, 'iam: {table}: запись с таким ключом уже есть'); END")


_PERSONA = "(SELECT value FROM meta WHERE key = 'persona_id')"
_NORM = "replace(replace(NEW.text, 'ё', 'е'), 'Ё', 'Е')"

V1 = Migration(1, "начальная схема: журнал, снимки, память+FTS5, входящие/исходящие, часы", tuple([
    # Сведения о личности этого файла: один файл — одна личность (R-024).
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT",
    "CREATE TRIGGER meta_fixed_update BEFORE UPDATE ON meta WHEN OLD.key IN ('persona_id', 'store_kind') "
    "BEGIN SELECT RAISE(ABORT, 'iam: persona_id и вид хранилища неизменяемы'); END",
    "CREATE TRIGGER meta_fixed_delete BEFORE DELETE ON meta WHEN OLD.key IN ('persona_id', 'store_kind') "
    "BEGIN SELECT RAISE(ABORT, 'iam: persona_id и вид хранилища неизменяемы'); END",

    # Журнал событий: только дописывается, seq без пропусков, одна личность.
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        persona_id TEXT NOT NULL,
        type TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        life_time_ms INTEGER NOT NULL CHECK (life_time_ms >= 0),
        source_kind TEXT NOT NULL,
        source_ref TEXT NOT NULL,
        causes TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        state_version INTEGER NOT NULL CHECK (state_version >= 0),
        changes_state INTEGER NOT NULL CHECK (changes_state IN (0, 1)),
        payload TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        body_hash TEXT NOT NULL
    ) STRICT""",
    "CREATE INDEX events_type ON events(type)",
    *_append_only("events"),
    _no_replace("events", "event_id = NEW.event_id OR idempotency_key = NEW.idempotency_key"),
    "CREATE TRIGGER events_seq_contiguous BEFORE INSERT ON events "
    "WHEN NEW.seq IS NOT (SELECT COALESCE(MAX(seq), 0) + 1 FROM events) "
    "BEGIN SELECT RAISE(ABORT, 'iam: seq должен идти подряд, без пропусков'); END",
    f"CREATE TRIGGER events_one_persona BEFORE INSERT ON events WHEN NEW.persona_id IS NOT {_PERSONA} "
    "BEGIN SELECT RAISE(ABORT, 'iam: событие другой личности'); END",

    # Снимки состояния по версиям (epoch, state_version): неизменяемы.
    """CREATE TABLE state_snapshots (
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        state_version INTEGER NOT NULL CHECK (state_version >= 0),
        at_seq INTEGER NOT NULL REFERENCES events(seq),
        life_time_ms INTEGER NOT NULL CHECK (life_time_ms >= 0),
        created_at TEXT NOT NULL,
        state TEXT NOT NULL,
        PRIMARY KEY (epoch, state_version)
    ) STRICT""",
    *_append_only("state_snapshots"),
    _no_replace("state_snapshots", "epoch = NEW.epoch AND state_version = NEW.state_version"),

    # Записи памяти: одна модель для всех слоёв и видов. Слой archive неизменяем (L-1).
    """CREATE TABLE memory_records (
        rid INTEGER PRIMARY KEY,
        memory_id TEXT NOT NULL UNIQUE,
        persona_id TEXT NOT NULL,
        layer TEXT NOT NULL CHECK (layer IN ('archive', 'personal', 'interpretation')),
        kind TEXT NOT NULL,
        origin TEXT NOT NULL,
        text TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        record TEXT NOT NULL
    ) STRICT""",
    "CREATE INDEX memory_layer_kind ON memory_records(layer, kind)",
    *_append_only("memory_records", when="OLD.layer = 'archive'"),
    _no_replace("memory_records", "memory_id = NEW.memory_id"),
    "CREATE TRIGGER memory_fixed_fields BEFORE UPDATE ON memory_records "
    "WHEN NEW.layer IS NOT OLD.layer OR NEW.memory_id IS NOT OLD.memory_id OR NEW.persona_id IS NOT OLD.persona_id "
    "BEGIN SELECT RAISE(ABORT, 'iam: слой, memory_id и persona_id записи памяти неизменяемы'); END",
    f"CREATE TRIGGER memory_one_persona BEFORE INSERT ON memory_records WHEN NEW.persona_id IS NOT {_PERSONA} "
    "BEGIN SELECT RAISE(ABORT, 'iam: запись памяти другой личности'); END",
    "CREATE TRIGGER memory_no_simulation BEFORE INSERT ON memory_records "
    "WHEN NEW.origin = 'simulation_event' AND (SELECT value FROM meta WHERE key = 'store_kind') = 'permanent' "
    "BEGIN SELECT RAISE(ABORT, 'iam: события симуляции не попадают в постоянное хранилище'); END",
    "CREATE VIEW archive AS SELECT * FROM memory_records WHERE layer = 'archive'",

    # Полнотекстовый поиск: unicode61 сворачивает регистр (в том числе кириллицу); ё приравнена к е.
    "CREATE VIRTUAL TABLE memory_fts USING fts5(text, tokenize = 'unicode61 remove_diacritics 2')",
    f"CREATE TRIGGER memory_fts_insert AFTER INSERT ON memory_records "
    f"BEGIN INSERT INTO memory_fts(rowid, text) VALUES (NEW.rid, {_NORM}); END",
    f"CREATE TRIGGER memory_fts_update AFTER UPDATE OF text ON memory_records "
    f"BEGIN UPDATE memory_fts SET text = {_NORM} WHERE rowid = NEW.rid; END",
    "CREATE TRIGGER memory_fts_delete AFTER DELETE ON memory_records "
    "BEGIN DELETE FROM memory_fts WHERE rowid = OLD.rid; END",

    # Входящие: содержимое сохраняется приёмником и не меняется; стадии восприятия — события.
    """CREATE TABLE inbox (
        input_id TEXT PRIMARY KEY,
        channel TEXT NOT NULL,
        external_id TEXT NOT NULL,
        arrived_seq INTEGER NOT NULL REFERENCES events(seq),
        received_at TEXT NOT NULL,
        sender TEXT NOT NULL,
        content TEXT NOT NULL,
        UNIQUE (channel, external_id)
    ) STRICT""",
    *_append_only("inbox"),
    _no_replace("inbox", "input_id = NEW.input_id OR (channel = NEW.channel AND external_id = NEW.external_id)"),

    # Исходящие: содержимое неизменно; статус requested → sent|failed|unknown, unknown → sent|failed.
    """CREATE TABLE outbox (
        action_id TEXT PRIMARY KEY,
        channel TEXT NOT NULL,
        recipient TEXT NOT NULL,
        content TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('requested', 'sent', 'failed', 'unknown')),
        requested_seq INTEGER NOT NULL REFERENCES events(seq),
        result_seq INTEGER REFERENCES events(seq),
        external_id TEXT
    ) STRICT""",
    "CREATE TRIGGER outbox_insert_requested BEFORE INSERT ON outbox WHEN NEW.status IS NOT 'requested' "
    "BEGIN SELECT RAISE(ABORT, 'iam: исходящее создаётся в статусе requested'); END",
    _no_replace("outbox", "action_id = NEW.action_id"),
    "CREATE TRIGGER outbox_status BEFORE UPDATE ON outbox "
    "WHEN NEW.content IS NOT OLD.content OR NEW.channel IS NOT OLD.channel OR NEW.recipient IS NOT OLD.recipient "
    "OR NEW.action_id IS NOT OLD.action_id OR NEW.requested_seq IS NOT OLD.requested_seq "
    "OR OLD.status NOT IN ('requested', 'unknown') OR NEW.status NOT IN ('sent', 'failed', 'unknown') "
    "OR (OLD.status = 'unknown' AND NEW.status = 'unknown') "
    "BEGIN SELECT RAISE(ABORT, 'iam: недопустимое изменение исходящего'); END",
    "CREATE TRIGGER outbox_no_delete BEFORE DELETE ON outbox "
    "BEGIN SELECT RAISE(ABORT, 'iam: outbox только дописывается (DELETE запрещён)'); END",

    # Служебная точка часов (heartbeat, docs/spec/time.md#life-clock): время жизни не убывает.
    """CREATE TABLE clock (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        life_time_ms INTEGER NOT NULL CHECK (life_time_ms >= 0),
        updated_at TEXT NOT NULL
    ) STRICT""",
    "CREATE TRIGGER clock_monotonic BEFORE UPDATE ON clock WHEN NEW.life_time_ms < OLD.life_time_ms "
    "BEGIN SELECT RAISE(ABORT, 'iam: время жизни не убывает'); END",
    "CREATE TRIGGER clock_no_delete BEFORE DELETE ON clock "
    "BEGIN SELECT RAISE(ABORT, 'iam: точку часов удалять нельзя'); END",
]))

MIGRATIONS: tuple[Migration, ...] = (V1,)
LATEST = MIGRATIONS[-1].version

_VERSION_TABLE = ("CREATE TABLE IF NOT EXISTS schema_version ("
                  "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL) STRICT")


def _tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {r[0] for r in rows}


def current_version(con: sqlite3.Connection) -> int:
    """Текущая версия формата: 0 — пустая база; UnknownDatabase — чужие таблицы без таблицы версий."""
    tables = _tables(con)
    if "schema_version" not in tables:
        if tables:
            raise UnknownDatabase(f"в базе есть таблицы {sorted(tables)}, но нет schema_version")
        return 0
    return con.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def migrate(con: sqlite3.Connection, *, target: int | None = None,
            migrations: tuple[Migration, ...] = MIGRATIONS,
            before: Callable[[int, int], None] | None = None) -> list[int]:
    """Довести формат до `target` (по умолчанию — последний). Возвращает применённые версии."""
    known = migrations[-1].version
    target = known if target is None else target
    have = current_version(con)
    if have > known:
        raise SchemaTooNew(f"формат базы v{have}, код знает только до v{known}")
    pending = [m for m in migrations if have < m.version <= target]
    if not pending:
        return []
    if before is not None and have > 0:
        before(have, target)
    applied = []
    con.guard.migrating = True
    try:
        for m in pending:
            with transaction(con):
                # Перечитываем версию внутри транзакции: параллельный мигратор не применит дважды.
                if current_version(con) >= m.version:
                    continue
                con.execute(_VERSION_TABLE)
                for stmt in m.statements:
                    con.execute(stmt)
                con.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                            (m.version, m.name, dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")))
            applied.append(m.version)
    finally:
        con.guard.migrating = False
    return applied
