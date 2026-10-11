"""Командная строка `iam`: копии, экспорт, восстановление, проверка (S06).

    iam backup  --db БАЗА [--backup-dir КАТАЛОГ] [--keep 3] [--reason ТЕКСТ]
    iam export  --db БАЗА --out ФАЙЛ.zip [--secret-env ИМЯ ...]
    iam restore КОПИЯ.db --to КАТАЛОГ --reason ТЕКСТ [--live-db БАЗА]
    iam verify  ПУТЬ        (база, копия, каталог копий или пакет экспорта)

Коды выхода: 0 — всё хорошо, 1 — найдены проблемы (копия повреждена, секрет, нет места…), 2 — неверный запуск.
Секреты в аргументы не передаются: `--secret-env` называет *переменную окружения*, значение берётся из неё.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

from iam.backup import (BackupError, NotEnoughSpace, SecretFound, SecretScanner, check_backup, create_backup,
                        default_backup_dir, export_persona, list_backups, open_for_write, restore_backup, verify_database,
                        verify_export)
from iam.backup.snapshots import KEEP_DEFAULT
from iam.state import StateController
from iam.storage import AlreadyLocked, StorageError


def _out(*parts) -> None:
    print(*parts)


def _err(*parts) -> None:
    print(*parts, file=sys.stderr)


def cmd_backup(args) -> int:
    db = Path(args.db)
    directory = Path(args.backup_dir) if args.backup_dir else default_backup_dir(db)
    store = controller = None
    try:   # если базой никто не владеет, запишем в журнал system.backup_created; иначе только сам снимок
        store = open_for_write(db, directory)
        controller = StateController(store, recover=False)
    except AlreadyLocked:
        _out("база занята работающим процессом: снимок сделан онлайн, запись о нём в журнал сделает он сам")
    except (StorageError, sqlite3.DatabaseError) as e:
        _err(f"не удалось открыть базу для записи в журнал ({e}); делаю только снимок")
    try:
        res = create_backup(db, directory, keep=args.keep, reason=args.reason or "", controller=controller)
    finally:
        if store is not None:
            store.close()
    m = res.info.manifest
    _out(f"снимок {m['snapshot_id']}: {res.info.path} ({m['size_bytes']} байт, журнал до seq={m['last_seq']}, "
         f"эпоха {m['epoch']}, версия {m['state_version']})")
    for p in res.rotated:
        _out(f"удалена старая копия: {p.name}")
    for p in res.corrupt:
        _err(f"ВНИМАНИЕ: повреждённая копия оставлена на месте: {p}")
    return 1 if res.corrupt else 0


def cmd_export(args) -> int:
    scanner = SecretScanner(use_env=True)
    for name in args.secret_env or []:
        value = os.environ.get(name)
        if not value:
            _err(f"переменная окружения {name} пуста или не задана")
            return 2
        scanner = SecretScanner([*scanner.known, value], use_env=False)
    res = export_persona(args.db, args.out, scanner=scanner)
    _out(f"экспорт: {res.path} (sha256 {res.sha256[:16]}…), записей: {res.counts}")
    if res.redactions:
        _out(f"удалено секретов: {res.redactions}")
    return 0


def cmd_restore(args) -> int:
    res = restore_backup(args.backup, args.to, reason=args.reason, live_db=args.live_db)
    _out(f"восстановлено в {res.path}: личность {res.persona_id}, эпоха {res.from_epoch} → {res.to_epoch}, "
         f"журнал до seq={res.restored_to_seq}, проверено на отдельной копии: {'да' if res.checked_on_copy else 'нет'}")
    if res.lost_seq_from is not None:
        _out(f"отброшены события живой базы seq={res.lost_seq_from}…{res.lost_seq_to}")
    _out("живая база не тронута; заменить её восстановленной — отдельный шаг оператора")
    return 0


def _verify_one(path: Path) -> list[str]:
    if zipfile.is_zipfile(path):
        return verify_export(path)
    if path.with_suffix(".json").is_file():
        rep = check_backup(path)
    else:
        rep = verify_database(path)
    for n in rep.notes:
        _out(f"  примечание: {n}")
    return rep.problems


def cmd_verify(args) -> int:
    path = Path(args.path)
    if path.is_dir():
        infos, broken = list_backups(path)
        targets = [i.path for i in infos] + broken
        if not targets:
            _err(f"в {path} нет копий")
            return 1
    elif path.exists():
        targets = [path]
    else:
        _err(f"{path}: нет такого пути")
        return 2
    bad = 0
    for t in targets:
        problems = _verify_one(t)
        _out(f"{'ОК   ' if not problems else 'ОШИБКА'} {t}")
        for p in problems:
            _out(f"  - {p}")
        bad += bool(problems)
    return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="iam", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="снимок базы (хранится три последних)")
    b.add_argument("--db", required=True)
    b.add_argument("--backup-dir")
    b.add_argument("--keep", type=int, default=KEEP_DEFAULT)
    b.add_argument("--reason")
    b.set_defaults(fn=cmd_backup)
    e = sub.add_parser("export", help="экспорт личности без секретов")
    e.add_argument("--db", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--secret-env", action="append", metavar="ИМЯ", help="имя переменной окружения с секретом")
    e.set_defaults(fn=cmd_export)
    r = sub.add_parser("restore", help="восстановить копию в отдельный каталог")
    r.add_argument("backup")
    r.add_argument("--to", required=True)
    r.add_argument("--reason", required=True)
    r.add_argument("--live-db")
    r.set_defaults(fn=cmd_restore)
    v = sub.add_parser("verify", help="проверить базу, копию, каталог копий или экспорт")
    v.add_argument("path")
    v.set_defaults(fn=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except SecretFound as e:
        _err(f"ОТКАЗ: {e}")
    except NotEnoughSpace as e:
        _err(f"ОТКАЗ: {e}")
    except (BackupError, StorageError, sqlite3.DatabaseError, FileExistsError, ValueError) as e:
        _err(f"ОШИБКА: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
