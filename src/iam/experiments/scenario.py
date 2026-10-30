"""Загрузка билетов S03 и временных профилей. Никакого переноса биографий в постоянную личность."""
from __future__ import annotations

import copy
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise ValueError(f"неверное логическое имя: {value!r}")
    return value


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    traits: dict
    motives: tuple[str, ...]

    @classmethod
    def load(cls, path: str | Path) -> "Persona":
        with Path(path).open("rb") as f:
            data = tomllib.load(f)
        identifier(data["id"])
        if data.get("format") != 1 or data.get("temporary") is not True:
            raise ValueError("нужен временный профиль формата 1")
        if not data.get("name") or not isinstance(data.get("traits"), dict):
            raise ValueError("нужны имя и черты профиля")
        motives = data.get("motives", [])
        if not isinstance(motives, list) or not all(isinstance(m, str) and m.strip() for m in motives):
            raise ValueError("motives — список непустых строк")
        return cls(data["id"], data["name"], data["traits"], tuple(motives))


@dataclass(frozen=True)
class Scenario:
    data: dict

    @classmethod
    def load(cls, path: str | Path) -> "Scenario":
        with Path(path).open("rb") as f:
            return cls.from_dict(tomllib.load(f))

    @classmethod
    def from_dict(cls, data: dict) -> "Scenario":
        """Каталог полностью проверяет tools/scenarios.py; здесь — исполнимое подмножество S11.

        Пятый decide намеренно допускается загрузчиком: раннер обязан остановиться до его вызова.
        Неподдерживаемые шаги отклоняются до создания файлов, а не молча пропускаются.
        """
        data = copy.deepcopy(data)
        identifier(data["id"])
        if data.get("format") != 1 or data.get("group") not in ("experiments", "motivation", "required"):
            raise ValueError("неверный формат или группа сценария")
        runs = data.get("run", [])
        ids = [identifier(r["id"]) for r in runs]
        if len(ids) != len(set(ids)) or len(ids) == 1:
            raise ValueError("нужны разные прогоны; одиночный run не используется")
        for run in runs:
            switches = run.get("switches", {})
            if any(k not in ("memory", "motivation") or v not in ("on", "off") for k, v in switches.items()):
                raise ValueError("неверные переключатели")
        steps = data.get("step", [])
        step_ids = [identifier(s["id"]) for s in steps]
        if not steps or len(step_ids) != len(set(step_ids)):
            raise ValueError("нужны шаги с уникальными именами")
        for item in [*steps, *data.get("background", [])]:
            if "run" in item and item["run"] not in ids:
                raise ValueError("ссылка на неизвестный прогон")
        for step in steps:
            kind = step["kind"]
            if kind not in ("session", "wait", "input", "world", "model", "decide"):
                raise ValueError(f"шаг {kind} требует исполнителя интеграционных сценариев S25")
            if kind == "session" and step["action"] not in ("start", "pause", "resume", "stop"):
                raise ValueError("аварийный сеанс требует исполнителя S25")
            if kind == "model" and step.get("delivery") != "once":
                raise ValueError("повторные/устаревшие ответы требуют исполнителя S25")
            if kind == "world" and step["outcome"] not in ("confirmed", "failed", "no_answer"):
                raise ValueError("неверный исход мира")
            if kind == "wait":
                values = [step[k] for k in ("life_ms", "calendar_ms") if k in step]
                if len(values) != 1 or type(values[0]) is not int or values[0] <= 0:
                    raise ValueError("ожидание: одно положительное целое время")
        return cls(data)

    @property
    def runs(self) -> list[dict]:
        return copy.deepcopy(self.data.get("run") or [{"id": "default"}])

    def items(self, name: str, run: str) -> list[dict]:
        return copy.deepcopy([x for x in self.data.get(name, []) if x.get("run", run) == run])
