"""Канонические трассы и таблица прогонов. Имена файлов не зависят от пользовательского текста."""
from __future__ import annotations

import csv
import copy
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

from iam.domain import canonical_json
from iam.domain.ids import PREFIXES


class Normalizer:
    """Случайные технические ID заменяются по порядку первого появления с сохранением всех ссылок.

    Исходные ID остаются в SQLite. Трасса — отдельный материал для сравнения, не резервная копия.
    Семя, профиль, тексты, версии, порядок, времена и исходы не стираются.
    """

    def __init__(self):
        self.names = {}
        self.counts = {}
        prefixes = "|".join(sorted(PREFIXES - {"per"}))
        self.pattern = re.compile(rf"\b({prefixes})_[A-Za-z0-9]{{8,40}}\b")
        self.diagnostics = {}

    # Смысловые данные никогда не используются как источник технических идентификаторов.
    RAW_FIELDS = frozenset({"text", "content", "params", "rationale", "explanation", "summary", "situation",
                            "input", "motives", "persona", "traits"})
    ID_FIELDS = frozenset({"event_id", "transition_id", "persona_id", "request_id", "proposal_id", "cycle_id",
                           "action_id", "memory_id", "session_id", "input_id", "moment_id", "snapshot_id"})

    def _replace(self, match):
        raw, prefix = match[0], match[1]
        if raw not in self.names:
            self.counts[prefix] = self.counts.get(prefix, 0) + 1
            self.names[raw] = f"{prefix}_{self.counts[prefix]:08d}"
        return self.names[raw]

    def _collect(self, value, field=None):
        if field in self.RAW_FIELDS:
            return
        if isinstance(value, dict):
            for k in sorted(value):
                self._collect(value[k], k)
        elif isinstance(value, (list, tuple)):
            for v in value:
                self._collect(v, field)
        elif field in self.ID_FIELDS and isinstance(value, str):
            match = self.pattern.fullmatch(value)
            if match:
                self._replace(match)
        elif field == "idempotency_key" and isinstance(value, str):
            for match in self.pattern.finditer(value):
                self._replace(match)

    def _convert(self, value, field=None, parent=None):
        if field in self.RAW_FIELDS or (field == "reason" and parent == "body"):
            return copy.deepcopy(value)
        if isinstance(value, str):
            if field == "idempotency_key" and re.fullmatch(r"err:[a-z_]+:[a-f0-9]{16}", value):
                # Контроллер хеширует сырой неверный ответ, включая случайные ID.
                # Причины ошибок сохраняются, служебному ключу даём воспроизводимый псевдоним.
                if value not in self.diagnostics:
                    self.diagnostics[value] = f"{value.rsplit(':', 1)[0]}:diagnostic{len(self.diagnostics) + 1:08d}"
                return self.diagnostics[value]
            return self.pattern.sub(lambda m: self.names.get(m[0], m[0]), value)
        if isinstance(value, dict):
            return {self._convert(k): self._convert(value[k], k, field) for k in sorted(value)}
        if isinstance(value, (list, tuple)):
            return [self._convert(v, field, parent) for v in value]
        return value

    def __call__(self, value):
        self._collect(value)
        return self._convert(value)


@dataclass(frozen=True)
class RunResult:
    path: Path
    trace: dict

    @property
    def trace_bytes(self) -> bytes:
        return (canonical_json(self.trace) + "\n").encode("utf-8")


class Results:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve() / "experiments" / "runs"

    def reserve(self) -> Path:
        """mkdir — атомарный резерв; уже сохранённый опыт не перезаписывается."""
        self.root.mkdir(parents=True, exist_ok=True)
        n = 1
        while True:
            path = self.root / f"run-{n:06d}"
            try:
                path.mkdir()
                return path
            except FileExistsError:
                n += 1

    def save(self, result: RunResult) -> None:
        raw = result.trace_bytes
        # Манифест пишется последним; незавершённые каталоги не попадают в сравнение.
        (result.path / "trace.json").write_bytes(raw)
        manifest = {k: result.trace[k] for k in ("scenario", "run", "persona", "architecture", "seed", "switches",
                                                "status", "decisions")}
        manifest["persona"] = result.trace["persona"]["id"]
        manifest["sha256"] = hashlib.sha256(raw).hexdigest()
        (result.path / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")

    def table(self) -> str:
        out = io.StringIO(newline="")
        fields = ["path", "scenario", "run", "persona", "architecture", "seed", "memory_off", "motivation_off",
                  "status", "decisions", "sha256"]
        writer = csv.DictWriter(out, fields, lineterminator="\n")
        writer.writeheader()
        for path in sorted(self.root.glob("run-*/manifest.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            raw = (path.parent / "trace.json").read_bytes()
            if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise ValueError(f"повреждена трасса: {path.parent}")
            switches = row.pop("switches")
            writer.writerow({"path": path.parent.name, **row, **switches})
        return out.getvalue()
