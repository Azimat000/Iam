"""Хранилище одной личности: один файл SQLite = одна личность (R-001, R-024).

Постоянная личность (`per_…`) и экспериментальные персонажи (`exp_…`) живут в разных файлах и разных
каталогах (`default_path`); вид хранилища записан в самой базе и не меняется. Писать может только
владелец замка (`WriterLock`); чтение (`readonly=True`) замка не требует.

Хранилище не решает, *что* записывать (это StateController, S05) — оно гарантирует, что записанное
целостно: журнал только дописывается, seq без пропусков, повтор ключа идемпотентности не создаёт
второго события, транзакция пишется целиком или никак.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from iam.domain import Event, MemoryRecord, Source, canonical_json, is_experimental, is_persona_id
from iam.domain.types import MEMORY_LAYERS

from . import db
from .errors import IdempotencyConflict, NotFound, PersonaMismatch, SchemaOutdated, SchemaTooNew, StorageError
from .lock import WriterLock
from .migrations import LATEST, current_version, migrate
from .runtime import check_runtime

DEFAULT_LAYERS = ("personal", "interpretation")  # обычное извлечение не видит архив (L-2)


def default_path(data_root: str | Path, persona_id: str) -> Path:
    """Где лежит файл личности: постоянные — `personas/`, экспериментальные — `experiments/`."""
    sub = "experiments" if is_experimental(persona_id) else "personas"
    return Path(data_root) / sub / f"{persona_id}.db"


def normalize_text(text: str) -> str:
    """Нормализация для поиска: ё → е (регистр сворачивает сам токенизатор unicode61)."""
    return text.replace("ё", "е").replace("Ё", "Е")


def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _body_hash(event: Event) -> str:
    return hashlib.sha256(canonical_json({"type": event.type, "payload": event.payload}).encode()).hexdigest()


def _abandon(con: db.Connection | None, lock: WriterLock | None) -> None:
    """Неудачное открытие: закрыть соединение и отпустить замок, ничего не оставляя висеть."""
    if con is not None:
        con.close()
    if lock is not None:
        lock.release()


@dataclass(frozen=True)
class Snapshot:
    epoch: int
    state_version: int
    at_seq: int
    life_time_ms: int
    created_at: str
    state: dict


@dataclass(frozen=True)
class InboxItem:
    input_id: str
    channel: str
    external_id: str
    arrived_seq: int
    received_at: str
    sender: str
    content: dict


@dataclass(frozen=True)
class OutboxItem:
    action_id: str
    channel: str
    recipient: str
    content: dict
    status: str
    requested_seq: int
    result_seq: int | None
    external_id: str | None


class Store:
    def __init__(self, con: db.Connection, path: Path, lock: WriterLock | None):
        self.con = con
        self.path = path
        self._lock = lock
        meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
        if "persona_id" not in meta:
            raise StorageError(f"{path}: в базе нет persona_id (создание личности не завершено)")
        self.persona_id: str = meta["persona_id"]
        self.store_kind: str = meta["store_kind"]
        self.created_at: str = meta["created_at"]

    # --- открытие и закрытие -------------------------------------------------------------------

    @classmethod
    def create(cls, path: str | Path, persona_id: str, *, created_at: str | None = None) -> "Store":
        """Создать файл новой личности. Вид хранилища определяется префиксом persona_id."""
        check_runtime()
        if not is_persona_id(persona_id):
            raise ValueError(f"неверный persona_id {persona_id!r}")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = WriterLock(path).acquire()
        con = None
        try:
            if path.exists():
                raise StorageError(f"{path} уже существует: одна личность — один файл")
            con = db.connect(path)
            migrate(con)
            kind = "experimental" if is_experimental(persona_id) else "permanent"
            with db.transaction(con):
                con.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [
                    ("persona_id", persona_id), ("store_kind", kind), ("created_at", created_at or _now_iso())])
            return cls(con, path, lock)
        except BaseException:
            _abandon(con, lock)
            raise

    @classmethod
    def open(cls, path: str | Path, *, readonly: bool = False, expect_persona: str | None = None,
             before_migrate=None) -> "Store":
        """Открыть существующую личность. Для записи берётся замок и выполняются миграции."""
        check_runtime()
        path = Path(path)
        if not path.exists():
            raise NotFound(f"{path}: файла личности нет")
        lock = None if readonly else WriterLock(path).acquire()
        con = None
        try:
            con = db.connect(path, readonly=readonly)
            if readonly:
                have = current_version(con)
                if have > LATEST:
                    raise SchemaTooNew(f"формат базы v{have}, код знает только до v{LATEST}")
                if have < LATEST:
                    raise SchemaOutdated(f"формат базы v{have}, нужна миграция до v{LATEST} (откройте для записи)")
            else:
                migrate(con, before=before_migrate)
            store = cls(con, path, lock)
            if expect_persona is not None and store.persona_id != expect_persona:
                raise PersonaMismatch(f"{path} принадлежит {store.persona_id}, а ожидалась {expect_persona}")
            return store
        except BaseException:
            _abandon(con, lock)
            raise

    @property
    def readonly(self) -> bool:
        return self._lock is None

    def close(self) -> None:
        self.con.close()
        if self._lock is not None:
            self._lock.release()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        """Атомарный блок для нескольких записей (например, все события одного перехода и снимок)."""
        self._writable()
        with db.transaction(self.con):
            yield self

    def _writable(self) -> None:
        if self._lock is None:
            raise StorageError("хранилище открыто только для чтения")

    # --- журнал событий ------------------------------------------------------------------------

    def last_seq(self) -> int:
        return self.con.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]

    def append_event(self, event: Event) -> tuple[Event, bool]:
        """Дописать событие. Возвращает (событие с seq, создано ли). Повтор ключа с тем же
        содержимым возвращает исходное событие; с другим — IdempotencyConflict."""
        self._writable()
        event.validate()
        if event.persona_id != self.persona_id:
            raise PersonaMismatch(f"событие {event.persona_id} в хранилище {self.persona_id}")
        if event.seq is not None:
            raise ValueError("seq присваивает журнал при фиксации; у нового события seq = None")
        body = _body_hash(event)
        with db.transaction(self.con):
            row = self.con.execute("SELECT seq, body_hash FROM events WHERE idempotency_key = ?",
                                   (event.idempotency_key,)).fetchone()
            if row is not None:
                if row["body_hash"] != body:
                    raise IdempotencyConflict(event.idempotency_key, row["seq"])
                return self.get_event(row["seq"]), False
            seq = self.last_seq() + 1
            self.con.execute(
                "INSERT INTO events (seq, event_id, persona_id, type, occurred_at, life_time_ms, source_kind,"
                " source_ref, causes, idempotency_key, epoch, state_version, changes_state, payload,"
                " schema_version, body_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, event.event_id, event.persona_id, event.type, event.occurred_at, event.life_time_ms,
                 event.source.kind, event.source.ref, json.dumps(list(event.causes)), event.idempotency_key,
                 event.epoch, event.state_version, int(event.changes_state), canonical_json(event.payload),
                 event.schema_version, body))
            self._touch_clock(event.life_time_ms, event.occurred_at)
        return event.with_seq(seq), True

    @staticmethod
    def _row_event(r: sqlite3.Row) -> Event:
        return Event(
            event_id=r["event_id"], persona_id=r["persona_id"], type=r["type"], occurred_at=r["occurred_at"],
            life_time_ms=r["life_time_ms"], source=Source(r["source_kind"], r["source_ref"]),
            idempotency_key=r["idempotency_key"], epoch=r["epoch"], state_version=r["state_version"],
            changes_state=bool(r["changes_state"]), payload=json.loads(r["payload"]),
            causes=tuple(json.loads(r["causes"])), seq=r["seq"], schema_version=r["schema_version"])

    def get_event(self, seq: int) -> Event:
        r = self.con.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        if r is None:
            raise NotFound(f"события seq={seq} нет")
        return self._row_event(r)

    def find_event(self, *, event_id: str | None = None, key: str | None = None) -> Event | None:
        """Найти событие по event_id или по ключу идемпотентности."""
        if (event_id is None) == (key is None):
            raise ValueError("укажите ровно одно: event_id или key")
        col, val = ("event_id", event_id) if event_id is not None else ("idempotency_key", key)
        r = self.con.execute(f"SELECT * FROM events WHERE {col} = ?", (val,)).fetchone()
        return None if r is None else self._row_event(r)

    def events(self, from_seq: int = 1, to_seq: int | None = None) -> Iterator[Event]:
        """События по порядку seq (порядок определяется только seq, а не временем)."""
        hi = self.last_seq() if to_seq is None else to_seq
        for r in self.con.execute("SELECT * FROM events WHERE seq BETWEEN ? AND ? ORDER BY seq", (from_seq, hi)):
            yield self._row_event(r)

    # --- снимки состояния ----------------------------------------------------------------------

    def put_snapshot(self, snap: Snapshot) -> None:
        self._writable()
        with db.transaction(self.con):
            self.con.execute(
                "INSERT INTO state_snapshots (epoch, state_version, at_seq, life_time_ms, created_at, state)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (snap.epoch, snap.state_version, snap.at_seq, snap.life_time_ms, snap.created_at,
                 canonical_json(snap.state)))

    @staticmethod
    def _row_snapshot(r: sqlite3.Row) -> Snapshot:
        return Snapshot(r["epoch"], r["state_version"], r["at_seq"], r["life_time_ms"], r["created_at"],
                        json.loads(r["state"]))

    def latest_snapshot(self) -> Snapshot | None:
        r = self.con.execute(
            "SELECT * FROM state_snapshots ORDER BY epoch DESC, state_version DESC LIMIT 1").fetchone()
        return None if r is None else self._row_snapshot(r)

    def get_snapshot(self, epoch: int, state_version: int) -> Snapshot:
        r = self.con.execute("SELECT * FROM state_snapshots WHERE epoch = ? AND state_version = ?",
                             (epoch, state_version)).fetchone()
        if r is None:
            raise NotFound(f"снимка ({epoch}, {state_version}) нет")
        return self._row_snapshot(r)

    # --- память --------------------------------------------------------------------------------

    def add_memory(self, rec: MemoryRecord) -> None:
        self._writable()
        rec.validate()
        if rec.persona_id != self.persona_id:
            raise PersonaMismatch(f"запись памяти {rec.persona_id} в хранилище {self.persona_id}")
        with db.transaction(self.con):
            self.con.execute(
                "INSERT INTO memory_records (memory_id, persona_id, layer, kind, origin, text, version, record)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.memory_id, rec.persona_id, rec.layer, rec.kind, rec.origin, rec.text, rec.version,
                 canonical_json(rec.to_dict())))

    def update_memory(self, rec: MemoryRecord) -> None:
        """Заменить запись слоя personal/interpretation (забывание, пересмотр). Архив не меняется."""
        self._writable()
        rec.validate()
        with db.transaction(self.con):
            old = self.get_memory(rec.memory_id)
            if old.layer == "archive":
                raise StorageError(f"{rec.memory_id}: запись архива неизменяема")
            if rec.layer != old.layer or rec.persona_id != old.persona_id:
                raise StorageError(f"{rec.memory_id}: слой и личность записи неизменяемы")
            if rec.version < old.version:
                raise StorageError(f"{rec.memory_id}: версия записи не может уменьшиться")
            self.con.execute(
                "UPDATE memory_records SET kind = ?, origin = ?, text = ?, version = ?, record = ?"
                " WHERE memory_id = ?",
                (rec.kind, rec.origin, rec.text, rec.version, canonical_json(rec.to_dict()), rec.memory_id))

    def get_memory(self, memory_id: str) -> MemoryRecord:
        r = self.con.execute("SELECT record FROM memory_records WHERE memory_id = ?", (memory_id,)).fetchone()
        if r is None:
            raise NotFound(f"записи памяти {memory_id} нет")
        return MemoryRecord.from_dict(json.loads(r["record"]))

    def search_memory(self, query: str, *, layers: tuple[str, ...] = DEFAULT_LAYERS,
                      limit: int = 20) -> list[MemoryRecord]:
        """Полнотекстовый поиск (все слова запроса, без учёта регистра, ё = е). По умолчанию —
        без архива: архив доступен только явным запросом (вмешательство оператора, S06/S18)."""
        bad = set(layers) - set(MEMORY_LAYERS)
        if bad or not layers:
            raise ValueError(f"неверные слои {sorted(bad) or layers!r}")
        terms = re.findall(r"\w+", normalize_text(query))
        if not terms:
            return []
        match = " ".join('"' + t + '"' for t in terms)
        marks = ",".join("?" * len(layers))
        rows = self.con.execute(
            "SELECT m.record FROM memory_fts f JOIN memory_records m ON m.rid = f.rowid"
            f" WHERE memory_fts MATCH ? AND m.layer IN ({marks}) ORDER BY bm25(memory_fts), m.rid LIMIT ?",
            (match, *layers, limit))
        return [MemoryRecord.from_dict(json.loads(r["record"])) for r in rows]

    # --- входящие и исходящие ------------------------------------------------------------------

    def add_inbox(self, item: InboxItem) -> tuple[InboxItem, bool]:
        """Сохранить входящее. Повторная доставка (тот же канал и внешний id) нового не создаёт."""
        self._writable()
        with db.transaction(self.con):
            old = self.find_inbox(item.channel, item.external_id)
            if old is not None:
                return old, False
            self.con.execute(
                "INSERT INTO inbox (input_id, channel, external_id, arrived_seq, received_at, sender, content)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item.input_id, item.channel, item.external_id, item.arrived_seq, item.received_at, item.sender,
                 canonical_json(item.content)))
        return item, True

    @staticmethod
    def _row_inbox(r: sqlite3.Row) -> InboxItem:
        return InboxItem(r["input_id"], r["channel"], r["external_id"], r["arrived_seq"], r["received_at"],
                         r["sender"], json.loads(r["content"]))

    def get_inbox(self, input_id: str) -> InboxItem:
        r = self.con.execute("SELECT * FROM inbox WHERE input_id = ?", (input_id,)).fetchone()
        if r is None:
            raise NotFound(f"входящего {input_id} нет")
        return self._row_inbox(r)

    def find_inbox(self, channel: str, external_id: str) -> InboxItem | None:
        r = self.con.execute("SELECT * FROM inbox WHERE channel = ? AND external_id = ?",
                             (channel, external_id)).fetchone()
        return None if r is None else self._row_inbox(r)

    def add_outbox(self, action_id: str, channel: str, recipient: str, content: dict, requested_seq: int) -> None:
        self._writable()
        with db.transaction(self.con):
            self.con.execute(
                "INSERT INTO outbox (action_id, channel, recipient, content, status, requested_seq)"
                " VALUES (?, ?, ?, ?, 'requested', ?)",
                (action_id, channel, recipient, canonical_json(content), requested_seq))

    def set_outbox_status(self, action_id: str, status: str, result_seq: int,
                          external_id: str | None = None) -> None:
        """requested → sent|failed|unknown; unknown → sent|failed (поздний ответ канала)."""
        self._writable()
        with db.transaction(self.con):
            self.get_outbox(action_id)
            self.con.execute("UPDATE outbox SET status = ?, result_seq = ?, external_id = ? WHERE action_id = ?",
                             (status, result_seq, external_id, action_id))

    def get_outbox(self, action_id: str) -> OutboxItem:
        r = self.con.execute("SELECT * FROM outbox WHERE action_id = ?", (action_id,)).fetchone()
        if r is None:
            raise NotFound(f"исходящего {action_id} нет")
        return OutboxItem(r["action_id"], r["channel"], r["recipient"], json.loads(r["content"]), r["status"],
                          r["requested_seq"], r["result_seq"], r["external_id"])

    # --- часы ----------------------------------------------------------------------------------

    def _touch_clock(self, life_time_ms: int, at: str) -> None:
        self.con.execute(
            "INSERT INTO clock (id, life_time_ms, updated_at) VALUES (1, ?, ?) ON CONFLICT (id) DO UPDATE SET"
            " life_time_ms = MAX(life_time_ms, excluded.life_time_ms), updated_at = excluded.updated_at",
            (life_time_ms, at))

    def heartbeat(self, life_time_ms: int, at: str) -> None:
        """Контрольная точка времени жизни (не событие журнала, docs/spec/time.md#life-clock)."""
        self._writable()
        with db.transaction(self.con):
            self._touch_clock(life_time_ms, at)

    def clock(self) -> tuple[int, str] | None:
        r = self.con.execute("SELECT life_time_ms, updated_at FROM clock WHERE id = 1").fetchone()
        return None if r is None else (r["life_time_ms"], r["updated_at"])

    # --- целостность ---------------------------------------------------------------------------

    def integrity_problems(self) -> list[str]:
        """Проверка целостности: страницы SQLite, индекс FTS, непрерывность seq."""
        probs = []
        res = [r[0] for r in self.con.execute("PRAGMA integrity_check")]
        if res != ["ok"]:
            probs += [f"integrity_check: {x}" for x in res]
        n, hi = self.con.execute("SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM events").fetchone()
        if n != hi:
            probs.append(f"в журнале пропуски seq: событий {n}, последний seq {hi}")
        if not self.readonly:
            try:
                self.con.execute("INSERT INTO memory_fts (memory_fts) VALUES ('integrity-check')")
            except sqlite3.DatabaseError as e:
                probs.append(f"FTS: {e}")
        return probs
