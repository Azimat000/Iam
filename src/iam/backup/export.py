"""Ручной экспорт личности без секретов (R-202, R-204, R-219).

Пакет — один zip-файл с JSON-строками: сведения о личности, журнал событий, снимки состояния, записи памяти
(все слои, в том числе архив: экспорт — полная копия содержания для оператора), входящие, исходящие. Читается из
базы одной согласованной транзакцией, не останавливая писателя. Секретов в пакете быть не должно:

1. строки данных проходят через `SecretScanner.redact` (токены, ключи, «канарейки», секреты окружения заменяются
   меткой; число замен по видам — в манифесте);
2. готовый пакет целиком проверяется ещё раз (`find` по каждому файлу, включая JSON-экранированные формы);
   найден секрет — пакет не создаётся (`SecretFound`), в сообщении только вид и место, без значения.

Экспорт — для переноса и чтения, не для восстановления: точное восстановление — из копий (`restore`).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BackupCorrupt, SecretFound
from .scan import SecretScanner
from .snapshots import _fsync_dir, _fsync_file, ro_connect
from .space import SpaceGuard

FORMAT = 1
KIND = "iam-export"
# таблица → порядок строк. FTS-индекс — производное, не экспортируется.
TABLES: dict[str, str] = {
    "meta": "key", "schema_version": "version", "events": "seq", "state_snapshots": "epoch, state_version",
    "memory_records": "rid", "inbox": "input_id", "outbox": "action_id", "clock": "id",
}
JSON_COLUMNS = {"events": ("payload", "causes"), "state_snapshots": ("state",), "memory_records": ("record",),
                "inbox": ("content",), "outbox": ("content",)}


@dataclass
class ExportResult:
    path: Path
    sha256: str
    counts: dict[str, int]
    redactions: dict[str, int] = field(default_factory=dict)


def _line(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rows(con: sqlite3.Connection, table: str) -> list[dict]:
    out = []
    for r in con.execute(f"SELECT * FROM {table} ORDER BY {TABLES[table]}"):
        row = dict(r)
        for col in JSON_COLUMNS.get(table, ()):
            row[col] = json.loads(row[col])    # вложенный JSON — структурой, чтобы редактирование видело строки
        out.append(row)
    return out


def export_persona(db_path: str | Path, out_path: str | Path, *, scanner: SecretScanner | None = None,
                   space: SpaceGuard | None = None, now: dt.datetime | None = None) -> ExportResult:
    """Собрать пакет экспорта `out_path` (существующий файл не перезаписывается)."""
    db_path, out_path = Path(db_path), Path(out_path)
    scanner = scanner or SecretScanner()
    if out_path.exists():
        raise FileExistsError(f"{out_path} уже существует")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    (space or SpaceGuard(out_path.parent)).ensure(db_path.stat().st_size * 2)
    now = now or dt.datetime.now(dt.UTC)
    con = ro_connect(db_path)
    try:
        con.execute("BEGIN")     # одна согласованная картина базы на всё чтение
        data = {t: _rows(con, t) for t in TABLES}
    except sqlite3.DatabaseError as e:
        raise BackupCorrupt(db_path, [f"не удалось прочитать базу: {e}"]) from e
    finally:
        con.close()
    meta = {r["key"]: r["value"] for r in data["meta"]}
    redactions: dict[str, int] = {}
    files: dict[str, bytes] = {}
    for table, rows in data.items():
        files[f"{table}.jsonl"] = "".join(_line(scanner.redact_value(r, redactions)) + "\n" for r in rows).encode()
    counts = {t: len(rows) for t, rows in data.items()}
    manifest = {
        "format": FORMAT, "kind": KIND, "persona_id": meta.get("persona_id"), "store_kind": meta.get("store_kind"),
        "exported_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": max((r["version"] for r in data["schema_version"]), default=0),
        "last_seq": max((r["seq"] for r in data["events"]), default=0), "counts": counts,
        "redactions": redactions, "secrets_checked": True,
        "files": {n: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)} for n, b in sorted(files.items())},
    }
    files["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    leaks = _scan_files(files, scanner)
    if leaks:
        raise SecretFound(leaks)
    tmp = out_path.with_name(f".{out_path.name}.partial")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name in sorted(files):
                z.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), files[name])
        _fsync_file(tmp)
        os.replace(tmp, out_path)
        _fsync_dir(out_path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return ExportResult(out_path, hashlib.sha256(out_path.read_bytes()).hexdigest(), counts, redactions)


def _scan_files(files: dict[str, bytes], scanner: SecretScanner) -> list[str]:
    """Проверка готового содержимого: и как текста, и в раскодированном JSON (экранированные формы)."""
    leaks = []
    for name, raw in sorted(files.items()):
        text = raw.decode("utf-8")
        leaks += scanner.describe(text, name)
        if name.endswith(".jsonl"):
            for n, line in enumerate(text.splitlines(), 1):
                strings: list[str] = []
                _collect_strings(json.loads(line), strings)
                for s in strings:
                    leaks += scanner.describe(s, f"{name}:{n}")
    return leaks


def _collect_strings(value, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for v in value:
            _collect_strings(v, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            _collect_strings(k, out)
            _collect_strings(v, out)


def verify_export(path: str | Path, *, scanner: SecretScanner | None = None) -> list[str]:
    """Проверка пакета: zip цел, файлы совпадают с манифестом по sha256 и числу строк, журнал без пропусков,
    секретов нет. Пустой список — пакет в порядке."""
    path = Path(path)
    scanner = scanner or SecretScanner()
    problems: list[str] = []
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        return [f"не открывается как zip: {e}"]
    with z:
        bad = z.testzip()
        if bad:
            return [f"повреждён файл {bad}"]
        names = set(z.namelist())
        if "manifest.json" not in names:
            return ["нет manifest.json"]
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("kind") != KIND or manifest.get("format") != FORMAT:
            return [f"это не пакет экспорта формата {FORMAT}"]
        files = {n: z.read(n) for n in names}
    for name, info in manifest["files"].items():
        raw = files.get(name)
        if raw is None:
            problems.append(f"нет файла {name}")
        elif hashlib.sha256(raw).hexdigest() != info["sha256"] or len(raw) != info["bytes"]:
            problems.append(f"{name}: не совпадает с манифестом")
    for table, n in manifest["counts"].items():
        raw = files.get(f"{table}.jsonl")
        if raw is not None and len(raw.decode("utf-8").splitlines()) != n:
            problems.append(f"{table}: строк не {n}")
    if "events.jsonl" in files:
        seqs = [json.loads(line)["seq"] for line in files["events.jsonl"].decode("utf-8").splitlines()]
        if seqs != list(range(1, len(seqs) + 1)):
            problems.append("в журнале пропуски или нарушен порядок seq")
    problems += [f"секрет: {x}" for x in _scan_files(files, scanner)]
    return problems
