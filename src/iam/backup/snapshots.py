"""Снимки базы: онлайн-копия SQLite, проверка, ротация до трёх, копия перед миграцией (R-200, R-201).

Снимок делается через онлайн-API SQLite (`Connection.backup`) отдельным соединением «только чтение» по пути к
базе: пишущий процесс не останавливается и ничего не знает о копии. Готовится под именем `.….partial`, проверяется
(контрольная сумма, `integrity_check`, а для актуального формата — полное воспроизведение журнала) и только потом
одним шагом получает настоящее имя; рядом лежит манифест `.json`. Ротация идёт **после** успешного снимка и
считает только целые копии: повреждённая не вытесняет хорошие и не удаляется молча — она в списке `corrupt`.

Копия защищает от ошибок программы и порчи файла; от потери самого компьютера нужна копия на другом устройстве.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from iam.domain import new_id
from iam.state import EventSpec, verify
from iam.storage import LATEST, Store, StorageError

from .errors import BackupCorrupt, BackupError
from .space import SpaceGuard, is_disk_full

FORMAT = 1
KEEP_DEFAULT = 3
KINDS = ("regular", "pre_migration")
PRE_MIGRATION_DIR = "pre-migration"


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    manifest: dict

    @property
    def snapshot_id(self) -> str:
        return self.manifest["snapshot_id"]

    @property
    def manifest_path(self) -> Path:
        return manifest_path(self.path)


@dataclass
class BackupResult:
    info: BackupInfo
    rotated: list[Path] = field(default_factory=list)   # удалённые ротацией старые копии
    corrupt: list[Path] = field(default_factory=list)   # повреждённые копии, оставленные на месте
    event_id: str | None = None                          # system.backup_created, если передан контроллер


@dataclass
class VerifyReport:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # не ошибки: например, формат старее, глубокая проверка позже

    @property
    def ok(self) -> bool:
        return not self.problems


def manifest_path(db_file: Path) -> Path:
    return db_file.with_suffix(".json")


def _stamp(now: dt.datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%S%fZ")


def _iso(now: dt.datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ro_connect(path: str | Path) -> sqlite3.Connection:
    """Обычное соединение «только чтение» (без сторожа ядра: копии и проверки не пишут в базу)."""
    con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.partial")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    _fsync_file(tmp)
    os.replace(tmp, path)


@contextlib.contextmanager
def dir_lock(directory: Path):
    """Один создатель копий на каталог: параллельные снимки и ротация не мешают друг другу."""
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def default_backup_dir(db_path: str | Path) -> Path:
    """`<корень>/backups/<личность>` для базы `<корень>/personas|experiments/<личность>.db`; иначе рядом с базой."""
    db = Path(db_path).resolve()
    if db.parent.name in ("personas", "experiments"):
        return db.parent.parent / "backups" / db.stem
    return db.parent / "backups" / db.stem


def _facts(path: Path) -> dict:
    """Сведения из самой базы копии: личность, формат, место в журнале, эпоха и версия состояния."""
    con = ro_connect(path)
    try:
        meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
        version = con.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        last_seq = con.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        snap = con.execute("SELECT epoch, state_version FROM state_snapshots "
                           "ORDER BY epoch DESC, state_version DESC LIMIT 1").fetchone()
    except sqlite3.DatabaseError as e:
        raise BackupCorrupt(path, [f"не удалось прочитать базу копии: {e}"]) from e
    finally:
        con.close()
    if "persona_id" not in meta:
        raise BackupCorrupt(path, ["в базе нет persona_id"])
    return {"persona_id": meta["persona_id"], "store_kind": meta.get("store_kind", "permanent"),
            "schema_version": version, "last_seq": last_seq,
            "epoch": snap[0] if snap else 1, "state_version": snap[1] if snap else 0}


def verify_database(path: str | Path, *, deep: bool = True) -> VerifyReport:
    """Проверка файла базы (живой или копии): целостность SQLite, а для актуального формата — индекс поиска,
    непрерывность журнала и воспроизведение журнала со сверкой снимков. Файл не изменяется."""
    path = Path(path)
    rep = VerifyReport()
    if not path.is_file():
        rep.problems.append(f"{path}: файла нет")
        return rep
    try:
        con = ro_connect(path)
        try:
            res = [r[0] for r in con.execute("PRAGMA integrity_check")]
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        rep.problems.append(f"файл не читается как база SQLite: {e}")
        return rep
    if res != ["ok"]:
        rep.problems += [f"integrity_check: {x}" for x in res]
        return rep
    try:
        facts = _facts(path)
    except BackupCorrupt as e:
        rep.problems += e.problems
        return rep
    if facts["schema_version"] > LATEST:
        rep.problems.append(f"формат базы v{facts['schema_version']} новее, чем знает код (v{LATEST})")
        return rep
    if not deep:
        return rep
    if facts["schema_version"] < LATEST:
        rep.notes.append(f"формат v{facts['schema_version']} старее v{LATEST}: журнал проверяется после миграции")
        return rep
    try:
        with Store.open(path, readonly=True) as store:
            rep.problems += store.integrity_problems()
            rep.problems += verify(store)
    except (StorageError, sqlite3.DatabaseError) as e:
        rep.problems.append(f"база не открылась для проверки: {e}")
    return rep


def load_info(db_file: str | Path) -> BackupInfo:
    """Манифест копии; нет или не читается — `BackupCorrupt`."""
    db_file = Path(db_file)
    mp = manifest_path(db_file)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BackupCorrupt(db_file, [f"нет читаемого манифеста {mp.name}: {e}"]) from e
    missing = [k for k in ("format", "snapshot_id", "persona_id", "sha256", "size_bytes", "last_seq", "kind")
               if k not in manifest]
    if missing or manifest["format"] != FORMAT:
        raise BackupCorrupt(db_file, [f"манифест {mp.name}: нет полей {missing}" if missing
                                      else f"неизвестный формат манифеста {manifest['format']}"])
    return BackupInfo(db_file, manifest)


def check_backup(db_file: str | Path, *, deep: bool = True) -> VerifyReport:
    """Проверка копии: манифест, размер и sha256, содержимое сходится с манифестом, затем `verify_database`."""
    db_file = Path(db_file)
    try:
        info = load_info(db_file)
    except BackupCorrupt as e:
        return VerifyReport(list(e.problems))
    rep = VerifyReport()
    if not db_file.is_file():
        rep.problems.append(f"файла копии {db_file.name} нет")
        return rep
    size = db_file.stat().st_size
    if size != info.manifest["size_bytes"]:
        rep.problems.append(f"размер {size} не равен записанному {info.manifest['size_bytes']}")
    if sha256_file(db_file) != info.manifest["sha256"]:
        rep.problems.append("контрольная сумма sha256 не совпадает с манифестом")
    if rep.problems:
        return rep
    inner = verify_database(db_file, deep=deep)
    rep.problems += inner.problems
    rep.notes += inner.notes
    if not inner.problems:
        facts = _facts(db_file)
        for key in ("persona_id", "last_seq", "epoch", "state_version"):
            if facts[key] != info.manifest.get(key):
                rep.problems.append(f"{key}: в базе {facts[key]!r}, в манифесте {info.manifest.get(key)!r}")
    return rep


def list_backups(backup_dir: str | Path) -> tuple[list[BackupInfo], list[Path]]:
    """Копии каталога по возрастанию времени и файлы, у которых нет читаемого манифеста."""
    backup_dir = Path(backup_dir)
    infos, broken = [], []
    for db_file in sorted(backup_dir.glob("*.db")) if backup_dir.is_dir() else []:
        try:
            infos.append(load_info(db_file))
        except BackupCorrupt:
            broken.append(db_file)
    infos.sort(key=lambda i: (i.manifest.get("created_at", ""), i.path.name))
    return infos, broken


def _rotate(directory: Path, keep: int) -> tuple[list[Path], list[Path]]:
    infos, broken = list_backups(directory)
    healthy, corrupt = [], list(broken)
    for info in infos:
        if check_backup(info.path, deep=False).ok:
            healthy.append(info)
        else:
            corrupt.append(info.path)
    removed = []
    for info in healthy[:-keep] if keep < len(healthy) else []:
        info.path.unlink(missing_ok=True)
        info.manifest_path.unlink(missing_ok=True)
        removed.append(info.path)
    return removed, corrupt


def _copy_database(src_path: Path, dst_path: Path, *, journal_delete: bool = False) -> None:
    """Онлайн-копия SQLite. «Диск полон» пробрасывается как есть (его понимает `is_disk_full`); всё остальное —
    нечитаемый источник, то есть `BackupCorrupt`."""
    try:
        src = ro_connect(src_path)
        try:
            dst = sqlite3.connect(dst_path)
            try:
                src.backup(dst)
                if journal_delete:
                    dst.execute("PRAGMA journal_mode = DELETE")   # копия — один самодостаточный файл
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.DatabaseError as e:
        if is_disk_full(e):
            raise
        raise BackupCorrupt(src_path, [f"база не читается: {e}"]) from e


def _cleanup(*paths: Path) -> None:
    for p in paths:
        for q in (p, Path(f"{p}-wal"), Path(f"{p}-shm"), Path(f"{p}-journal")):
            q.unlink(missing_ok=True)


def create_backup(db_path: str | Path, backup_dir: str | Path, *, kind: str = "regular", reason: str = "",
                  keep: int = KEEP_DEFAULT, space: SpaceGuard | None = None, controller=None,
                  now: dt.datetime | None = None, migration: tuple[int, int] | None = None) -> BackupResult:
    """Снимок базы в `backup_dir` (копии перед миграцией — в `backup_dir/pre-migration`, ротация там своя).

    Ошибка на любом шаге оставляет каталог как был: недоделанный снимок удаляется, прежние копии целы.
    С `controller` (процесс, который владеет базой) в журнал личности пишется `system.backup_created`."""
    if kind not in KINDS:
        raise ValueError(f"вид копии {kind!r}: допустимо {KINDS}")
    if keep < 1:
        raise ValueError("хранить нужно хотя бы одну копию")
    db_path = Path(db_path)
    if not db_path.is_file():
        raise BackupError(f"{db_path}: файла базы нет")
    directory = Path(backup_dir) / PRE_MIGRATION_DIR if kind == "pre_migration" else Path(backup_dir)
    now = now or dt.datetime.now(dt.UTC)
    size = db_path.stat().st_size + sum(p.stat().st_size for p in (Path(f"{db_path}-wal"),) if p.exists())
    (space or SpaceGuard(directory.parent if not directory.exists() else directory)).ensure(size)
    with dir_lock(directory):
        for stale in directory.glob(".*.partial"):    # брошенные недоделанные снимки прошлых сбоев
            stale.unlink(missing_ok=True)
        snapshot_id = new_id("snp")
        partial = directory / f".{snapshot_id}.partial"
        final = None
        try:
            _copy_database(db_path, partial, journal_delete=True)
            _fsync_file(partial)
            rep = verify_database(partial)
            if not rep.ok:
                raise BackupCorrupt(partial, rep.problems)
            facts = _facts(partial)
            final = directory / f"{facts['persona_id']}-{_stamp(now)}-{snapshot_id}.db"
            manifest = {
                "format": FORMAT, "snapshot_id": snapshot_id, "kind": kind, "reason": reason,
                "created_at": _iso(now), "sha256": sha256_file(partial), "size_bytes": partial.stat().st_size,
                **facts}
            if migration is not None:
                manifest["migration"] = {"from": migration[0], "to": migration[1]}
            os.replace(partial, final)
            _write_json_atomic(manifest_path(final), manifest)
            _fsync_dir(directory)
        except BaseException:
            _cleanup(partial)
            if final is not None:
                final.unlink(missing_ok=True)
                manifest_path(final).unlink(missing_ok=True)
            raise
        rotated, corrupt = _rotate(directory, keep)
    result = BackupResult(BackupInfo(final, manifest), rotated, corrupt)
    if controller is not None:
        res = controller.record_event(EventSpec("system.backup_created", f"bak:{snapshot_id}", {
            "snapshot_id": snapshot_id, "file": final.name, "kind": kind, "sha256": manifest["sha256"],
            "last_seq": manifest["last_seq"], "schema_version": manifest["schema_version"]}))
        result.event_id = res.event.event_id if res.event is not None else None
    return result


def migration_hook(db_path: str | Path, backup_dir: str | Path, **kw) -> Callable[[int, int], None]:
    """Хук `before` мигратора (R-200): перед изменением формата обязательно делается свежая проверенная копия.
    Не удалась копия (нет места, повреждена база) — исключение, миграция не начинается, база не тронута."""
    def before(have: int, target: int) -> None:
        create_backup(db_path, backup_dir, kind="pre_migration", reason=f"перед миграцией формата v{have} → v{target}",
                      migration=(have, target), **kw)
    return before


def open_for_write(db_path: str | Path, backup_dir: str | Path | None = None, **kw) -> Store:
    """Открыть личность для записи; если нужна миграция формата — сначала копия в `pre-migration`."""
    backup_dir = default_backup_dir(db_path) if backup_dir is None else backup_dir
    return Store.open(db_path, before_migrate=migration_hook(db_path, backup_dir, **kw))
