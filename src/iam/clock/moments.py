"""Плановые моменты (docs/spec/time.md#moments): чистые функции над записями моментов.

Момент — `{moment_id, kind, due_*, owner_ref, fired_count}`. `kind = life` — срок во времени жизни
(основной вид: наступает только пока время жизни идёт); `kind = calendar` — срок в календарном времени
(только факты внешнего мира; если срок прошёл в паузе, после возобновления наступит один раз с `overdue_ms`).
Записи лежат в состоянии (`world_time.moments`) и меняются переходами; здесь только описание и расчёт
«что наступило», без записи и без фонового опроса.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .lifeclock import calendar_diff_ms

MOMENT_ID_RE = re.compile(r"^mom_[A-Za-z0-9]{8,40}$")
_CAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


def life_moment(moment_id: str, due_life_ms: int, owner_ref: str) -> dict:
    return {"moment_id": moment_id, "kind": "life", "due_life_ms": due_life_ms, "owner_ref": owner_ref,
            "fired_count": 0}


def calendar_moment(moment_id: str, due_calendar: str, owner_ref: str) -> dict:
    return {"moment_id": moment_id, "kind": "calendar", "due_calendar": due_calendar, "owner_ref": owner_ref,
            "fired_count": 0}


def moment_errors(m: object) -> list[str]:
    """Ошибки записи момента (пустой список — запись верна)."""
    if not isinstance(m, dict):
        return ["момент должен быть объектом"]
    errs = []
    if not isinstance(m.get("moment_id"), str) or not MOMENT_ID_RE.match(m["moment_id"]):
        errs.append("moment_id: нужен идентификатор mom_…")
    if not isinstance(m.get("owner_ref"), str) or not m["owner_ref"]:
        errs.append("owner_ref: нужна непустая строка")
    if m.get("fired_count") != 0 or isinstance(m.get("fired_count"), bool):
        errs.append("fired_count: у нового момента 0")
    kind = m.get("kind")
    if kind == "life":
        due = m.get("due_life_ms")
        if not isinstance(due, int) or isinstance(due, bool) or due < 0:
            errs.append("due_life_ms: нужно целое ≥ 0")
        extra = set(m) - {"moment_id", "kind", "due_life_ms", "owner_ref", "fired_count"}
    elif kind == "calendar":
        if not isinstance(m.get("due_calendar"), str) or not _CAL_RE.match(m["due_calendar"]):
            errs.append("due_calendar: нужна метка UTC ISO 8601")
        extra = set(m) - {"moment_id", "kind", "due_calendar", "owner_ref", "fired_count"}
    else:
        return errs + [f"kind: {kind!r} — нужно life или calendar"]
    if extra:
        errs.append(f"лишние поля момента: {sorted(extra)}")
    return errs


@dataclass(frozen=True)
class DueMoment:
    moment: dict
    n: int            # номер срабатывания (для ключа `due:<moment_id>:<n>`)
    overdue_ms: int   # насколько срок уже прошёл (для life — в мс жизни, для calendar — в календарных мс)


def due_moments(moments: dict, life_ms: int, calendar_now: str) -> list[DueMoment]:
    """Наступившие моменты в детерминированном порядке: сначала life, затем calendar; внутри — по сроку и id.
    Вызывать только пока сеанс идёт: на паузе время жизни стоит, а календарные сроки сработают
    один раз после возобновления."""
    due = []
    for m in moments.values():
        if m["kind"] == "life" and m["due_life_ms"] <= life_ms:
            due.append((0, m["due_life_ms"], m["moment_id"], DueMoment(m, m["fired_count"] + 1,
                                                                        life_ms - m["due_life_ms"])))
        elif m["kind"] == "calendar":
            late = calendar_diff_ms(calendar_now, m["due_calendar"])
            if late >= 0:
                due.append((1, m["due_calendar"], m["moment_id"], DueMoment(m, m["fired_count"] + 1, late)))
    due.sort(key=lambda t: t[:3])
    return [t[3] for t in due]
