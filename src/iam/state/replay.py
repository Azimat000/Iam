"""Воспроизведение: состояние, пересобранное из журнала, должно совпасть со снимками (R-024, R-105).

Журнал событий — источник истины: каждое событие-носитель перехода (`payload.transition`) хранит операции
и пары (до, после). Воспроизведение применяет эти операции с нуля теми же функциями, что и контроллер, и
сверяет результат с записанными изменениями и со снимком каждой версии. Расхождение — повреждение или
обход контроллера. Эпоха меняется только восстановлением копии (D-013): вмешательство оператора
`restore_backup` открывает новую эпоху (+1) на той же версии состояния, `system.restored` закрепляет её, и в
этой точке есть свой снимок `(новая эпоха, та же версия)`. Любая другая смена эпохи — `ReplayError`.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Iterator

from iam.domain import Event, Op, Transition, canonical_json
from iam.storage import NotFound, Store

from . import rules
from .errors import ReplayError
from .paths import apply_op, empty_state


@dataclass
class ReplayResult:
    epoch: int
    state_version: int
    state: dict
    memory: dict[str, dict]            # memory_id → запись после всех переходов (только записанные переходами)
    last_seq: int
    snapshots_at: dict[tuple[int, int], int] = field(default_factory=dict)   # (эпоха, версия) → seq последнего события
    problems: list[str] = field(default_factory=list)


def _apply_transition(state: dict, memory: dict, t: Transition) -> list[dict]:
    changes = []
    for op in t.ops:
        if op.group == "memory":
            before = memory.get(op.path)
            memory[op.path] = copy.deepcopy(op.value)
            changes.append({"group": "memory", "path": op.path, "op": op.op, "before": before, "after": op.value})
        else:
            changes.append(apply_op(state, op, rules.MACHINES))
    return changes


def replay(store: Store) -> Iterator[ReplayResult]:
    """Пошагово воспроизвести журнал: после каждого перехода выдаётся `ReplayResult` (копия состояния
    на этой версии). Несовпадения записываются в `problems`; пустой список — журнал согласован."""
    state, memory, version, last_life = empty_state(), {}, 0, 0
    res = ReplayResult(1, 0, state, memory, 0)
    marks: dict[tuple[int, int], int] = {}
    res.snapshots_at = marks
    for ev in store.events():
        if ev.epoch != res.epoch:
            if not _opens_restored_epoch(ev, res.epoch, version):
                raise ReplayError(f"seq={ev.seq}: смена эпохи {res.epoch} → {ev.epoch} не восстановлением копии")
            res.epoch = ev.epoch
        if ev.life_time_ms < last_life:
            res.problems.append(f"seq={ev.seq}: время жизни убывает ({ev.life_time_ms} < {last_life})")
        last_life = max(last_life, ev.life_time_ms)
        if ev.changes_state:
            marks[(ev.epoch, ev.state_version)] = ev.seq
        if "transition" in ev.payload and ev.changes_state:
            version += 1
            res.last_seq = ev.seq
            t = Transition.from_dict(ev.payload["transition"])
            if ev.state_version != version:
                res.problems.append(f"seq={ev.seq}: версия {ev.state_version}, ожидалась {version}")
            if (t.base_epoch, t.base_version) != (res.epoch, version - 1):
                res.problems.append(f"seq={ev.seq}: переход основан на ({t.base_epoch}, {t.base_version})")
            changes = _apply_transition(state, memory, t)
            if canonical_json(changes) != canonical_json(ev.payload["changes"]):
                res.problems.append(f"seq={ev.seq}: записанные изменения не совпадают с воспроизведёнными")
            yield ReplayResult(res.epoch, version, copy.deepcopy(state), copy.deepcopy(memory), ev.seq, marks,
                               res.problems)
        elif ev.changes_state and ev.state_version != version:
            res.problems.append(f"seq={ev.seq}: событие перехода с версией {ev.state_version}, а переход — {version}")
        elif ev.type == "system.restored":
            res.last_seq = ev.seq
            if (ev.payload.get("from_epoch"), ev.payload.get("to_epoch")) != (ev.epoch - 1, ev.epoch):
                res.problems.append(f"seq={ev.seq}: system.restored с эпохами {ev.payload.get('from_epoch')} → "
                                    f"{ev.payload.get('to_epoch')}, а событие в эпохе {ev.epoch}")
            yield ReplayResult(res.epoch, version, copy.deepcopy(state), copy.deepcopy(memory), ev.seq, marks,
                               res.problems)


def _opens_restored_epoch(ev: Event, epoch: int, version: int) -> bool:
    """Эпоху открывает только вмешательство оператора «restore_backup» на той же версии состояния."""
    return (ev.epoch == epoch + 1 and ev.type == "operator.intervention" and ev.state_version == version
            and ev.payload.get("kind") == "restore_backup")


def verify(store: Store) -> list[str]:
    """Полная проверка согласованности: воспроизведение журнала с нуля, сверка со снимком каждой версии
    (состояние и место в журнале), с записями памяти и с последним снимком. Пустой список — всё сходится."""
    problems: list[str] = []
    final, snaps = None, {}
    try:
        for step in replay(store):
            final = step
            key = (step.epoch, step.state_version)
            try:
                snaps[key] = snap = store.get_snapshot(*key)
            except NotFound:
                problems.append(f"нет снимка версии {step.state_version} (эпоха {step.epoch})")
                continue
            if canonical_json(snap.state) != canonical_json(step.state):
                problems.append(f"снимок версии {step.state_version} (эпоха {step.epoch}) не совпадает "
                                f"с воспроизведённым состоянием")
    except ReplayError as e:
        problems.append(str(e))
    if final is None:
        latest = store.latest_snapshot()
        return problems + ([f"снимок есть, а переходов в журнале нет: версия {latest.state_version}"] if latest else [])
    problems += final.problems
    for (epoch, version), snap in snaps.items():   # последнее событие каждой версии известно после полного прохода
        if snap.at_seq != final.snapshots_at[(epoch, version)]:
            problems.append(f"снимок версии {version} (эпоха {epoch}): at_seq={snap.at_seq}, последнее событие "
                            f"seq={final.snapshots_at[(epoch, version)]}")
    latest = store.latest_snapshot()
    if latest is None or (latest.epoch, latest.state_version) != (final.epoch, final.state_version):
        problems.append("последний снимок не соответствует последнему переходу")
    for mid, rec in final.memory.items():
        try:
            have = store.get_memory(mid).to_dict()
        except NotFound:
            problems.append(f"запись памяти {mid} из журнала отсутствует в хранилище")
            continue
        if canonical_json(have) != canonical_json(rec):
            problems.append(f"запись памяти {mid} в хранилище не совпадает с журналом")
    return problems
