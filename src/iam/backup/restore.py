"""Восстановление копии (R-187, R-203): только в **отдельный каталог**, с проверкой на отдельной копии.

Живую базу восстановление не трогает никогда: из копии собирается новая база под черновым именем, на ней
проверяются целостность и воспроизведение журнала (это и есть «проверено на отдельной копии»), затем
`mark_restored` открывает новую эпоху и пишет `system.restored`, проверка повторяется — и только тогда база
появляется под настоящим именем (жёсткая ссылка, не перезаписывающая существующий файл). Заменить живую
базу восстановленной — отдельный осознанный шаг оператора вне этого модуля.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
import os

from iam.state import mark_restored, verify
from iam.storage import Store

from .errors import BackupCorrupt, RestoreRefused
from .snapshots import _cleanup, _copy_database, _fsync_dir, _fsync_file, _iso, check_backup, load_info, ro_connect
from .space import SpaceGuard

_BACKUP_FIELDS = ("snapshot_id", "kind", "created_at", "sha256", "last_seq", "epoch", "state_version",
                  "schema_version")


@dataclass(frozen=True)
class RestoreResult:
    path: Path
    snapshot_id: str
    persona_id: str
    from_epoch: int
    to_epoch: int
    restored_to_seq: int
    lost_seq_from: int | None
    lost_seq_to: int | None
    checked_on_copy: bool
    restored_event_id: str


def _live_last_seq(live_db: Path) -> int | None:
    try:
        con = ro_connect(live_db)
        try:
            return con.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return None


def restore_backup(backup_file: str | Path, target_dir: str | Path, *, reason: str, live_db: str | Path | None = None,
                   space: SpaceGuard | None = None, now: dt.datetime | None = None) -> RestoreResult:
    """Восстановить копию в `target_dir/<личность>.db`. Отказ до записи, если база по этому пути уже есть."""
    backup_file, target_dir = Path(backup_file), Path(target_dir)
    if not reason or not reason.strip():
        raise RestoreRefused("восстановление без причины не выполняется (это вмешательство оператора)")
    rep = check_backup(backup_file)
    if not rep.ok:
        raise BackupCorrupt(backup_file, rep.problems)
    info = load_info(backup_file)
    persona_id = info.manifest["persona_id"]
    target = target_dir / f"{persona_id}.db"
    if target.exists() or target.is_symlink():
        raise RestoreRefused(f"{target} уже существует: восстановление не перезаписывает базы")
    if live_db is not None and Path(live_db).resolve() == target.resolve():
        raise RestoreRefused("восстановление поверх живой базы не выполняется: укажите другой каталог")
    target_dir.mkdir(parents=True, exist_ok=True)
    (space or SpaceGuard(target_dir)).ensure(info.manifest["size_bytes"] * 2)
    draft = target_dir / f".{target.name}.restoring"
    _cleanup(draft, Path(f"{draft}.lock"))
    store = None
    try:
        _copy_database(backup_file, draft)
        store = Store.open(draft, expect_persona=persona_id)   # возможная миграция формата — на копии
        before = store.integrity_problems() + verify(store)
        if before:
            raise BackupCorrupt(backup_file, ["проверка на отдельной копии: " + p for p in before])
        lost_to = _live_last_seq(Path(live_db)) if live_db is not None else None
        _, restored = mark_restored(
            store, snapshot_id=info.snapshot_id, reason=reason, checked_on_copy=True,
            backup={k: info.manifest[k] for k in _BACKUP_FIELDS if k in info.manifest}, lost_seq_to=lost_to,
            now=_iso(now) if now is not None else None)
        after = store.integrity_problems() + verify(store)
        if after:
            raise BackupCorrupt(draft, ["после восстановления: " + p for p in after])
        store.close()
        store = None
        _fsync_file(draft)
        os.link(draft, target)   # атомарно, существующее не перезаписывает
        _fsync_dir(target_dir)
    except BaseException:
        if store is not None:
            store.close()
        raise
    finally:
        _cleanup(draft, Path(f"{draft}.lock"))
    p = restored.payload
    return RestoreResult(target, info.snapshot_id, persona_id, p["from_epoch"], p["to_epoch"], p["restored_to_seq"],
                         p.get("lost_seq_from"), p.get("lost_seq_to"), p["checked_on_copy"], restored.event_id)
