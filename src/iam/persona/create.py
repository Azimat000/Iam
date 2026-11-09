"""Рождение постоянной личности в новом каталоге, со снимком и проверкой восстановления."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from iam.backup import BackupError, SecretScanner, SpaceGuard, create_backup, default_backup_dir, restore_backup
from iam.backup.snapshots import _fsync_dir, _fsync_file
from iam.domain import new_id
from iam.state import StateController
from iam.storage import StorageError, Store, WriterLock, default_path
from iam.world import fingerprint

_TRAITS = ("curiosity", "sociability", "persistence")
_TEXT_LISTS = ("values", "boundaries", "inclinations")
_PROFILE_FIELDS = {"format", "label", "traits", *_TEXT_LISTS}
_EXPERIMENT_MARKER = re.compile(r"exp_[a-z0-9]{8,}|\b(?:exp|experiments)[/\\]|\bsimulation_event\b", re.I)
_MATERIALS = (
    ("pavement", "Вода между камнями",
     "После дождя углубления между камнями ещё держат воду, хотя верхушки камней уже сухие. "
     "Лужи на открытом месте отражают небо, а у стены в воде чаще видно тёмный край крыши."),
    ("letters", "Письмо из двух комнат",
     "В пустой комнате Лена читала письмо вполголоса. Из кухни тот же голос звучал тише, "
     "но слова всё ещё можно было разобрать. В письме стоял вопрос, на который она уже ответила вчера."),
    ("paper", "Бумага и сгиб",
     "Лист можно сложить, разгладить и использовать снова. Даже после разглаживания сгиб заметен "
     "при косом свете: две стороны отражают его под разными углами."),
)


@dataclass(frozen=True)
class Creation:
    persona_id: str
    database: Path
    backup: Path
    restored: Path
    birth_event_id: str
    snapshot_id: str


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def _initial(profile: dict, decision: str, birth: str) -> dict:
    if not isinstance(decision, str) or not re.fullmatch(r"D-[0-9]{3,}", decision):
        raise ValueError("Укажите номер решения о выборе характера (D-NNN).")
    if (not isinstance(profile, dict) or set(profile) != _PROFILE_FIELDS
            or type(profile["format"]) is not int or profile["format"] != 1
            or not _text(profile["label"], 80)):
        raise ValueError("Ожидается профиль характера формата 1 с точным набором полей.")
    traits = profile["traits"]
    if (not isinstance(traits, dict) or set(traits) != set(_TRAITS)
            or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in traits.values())):
        raise ValueError("Черты curiosity, sociability, persistence должны быть числами от 0 до 1.")
    for field in _TEXT_LISTS:
        entries = profile[field]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 6 or any(not _text(x, 160) for x in entries):
            raise ValueError(f"{field}: от одной до шести непустых строк до 160 знаков.")
    serialized = json.dumps(profile, ensure_ascii=False)
    if _EXPERIMENT_MARKER.search(serialized) or SecretScanner().find(serialized):
        raise ValueError("Профиль содержит экспериментальный идентификатор, отметку происхождения или секрет.")

    return {
        "traits": {
            "profile": {
                "format": 1, "label": profile["label"].strip(), "decision": decision, "origin": "operator_provided",
                "inclinations": [s.strip() for s in profile["inclinations"]],
            },
            "traits": [{"name": name, "value": traits[name], "source": birth} for name in _TRAITS],
            "values": [s.strip() for s in profile["values"]],
            "boundaries": [s.strip() for s in profile["boundaries"]],
        },
        "preferences": {"likes": [s.strip() for s in profile["inclinations"]]},
        "world_time": {"world": {
            "space": {
                "places": {"desk": "Рабочий стол", "shelf": "Библиотека", "room": "Личное пространство"},
                "objects": {"notebook": {"title": "Блокнот", "place_id": "desk"}},
            },
            "library": {
                key: {"title": title, "text": content, "origin": "operator_provided",
                      "fingerprint": fingerprint(content), "read": False}
                for key, title, content in _MATERIALS
            },
            "projects": {},
        }},
    }


def create(root: str | Path, profile: dict, *, decision: str) -> Creation:
    """Подготовить базу, исходную копию и проверочное восстановление, затем опубликовать вместе."""
    persona_id = new_id("per")
    birth = "evt_" + persona_id.removeprefix("per_")
    initial = _initial(profile, decision, birth)
    target = Path(root).expanduser()
    target = target.parent.resolve() / target.name
    if any(part.casefold() in {"exp", "experiments"} or _EXPERIMENT_MARKER.search(part)
           for part in target.parts):
        raise ValueError("Постоянной истории нужен отдельный каталог вне экспериментов.")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    with WriterLock(target.with_name(f".{target.name}.creation")):
        if target.exists() or target.is_symlink():
            raise FileExistsError("Каталог постоянной истории уже существует.")
        SpaceGuard(target.parent).ensure()
        with TemporaryDirectory(prefix=f".{target.name}.creating-", dir=target.parent) as temporary:
            draft = Path(temporary)
            db = default_path(draft, persona_id)
            with Store.create(db, persona_id) as store:
                controller = StateController(store)
                result = controller.create_persona(initial, reason=f"Начальный характер, решение {decision}",
                                                   actor_ref="iam_cli")
                if not result.applied:
                    raise StorageError(f"Рождение отклонено: {result.reason}")
                backup = create_backup(db, default_backup_dir(db), controller=controller,
                                       reason="Исходная копия постоянной личности")
            restored = restore_backup(backup.info.path, draft / "restore-check",
                                      reason="Проверка восстановления исходной копии")
            _fsync_file(db)
            for directory in (db.parent, backup.info.path.parent.parent, draft):
                _fsync_dir(directory)
            if target.exists() or target.is_symlink():
                raise FileExistsError("Каталог постоянной истории уже существует.")
            draft.rename(target)
            try:
                _fsync_dir(target.parent)
            except OSError as exc:
                raise OSError(f"Комплект опубликован в {target}. Синхронизация каталога не подтверждена: {exc}") from exc
            return Creation(persona_id, target / db.relative_to(draft),
                            target / backup.info.path.relative_to(draft),
                            target / restored.path.relative_to(draft), birth, restored.snapshot_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="новый каталог постоянной истории")
    parser.add_argument("--profile", type=Path, required=True, help="согласованный профиль характера (TOML)")
    parser.add_argument("--decision", required=True, help="решение пользователя о характере (D-NNN)")
    args = parser.parse_args()
    try:
        with args.profile.open("rb") as handle:
            profile = tomllib.load(handle)
        result = create(args.root, profile, decision=args.decision)
    except (ValueError, OSError, StorageError, BackupError, sqlite3.Error) as exc:
        parser.exit(1, f"Ошибка создания: {exc}\n")
    print(json.dumps({
        "persona_id": result.persona_id, "birth_event_id": result.birth_event_id, "snapshot_id": result.snapshot_id,
        "database": str(result.database), "backup": str(result.backup), "restored": str(result.restored),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
