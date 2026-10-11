"""Применение операций перехода (`set | add | remove | transition`) к группам состояния.

Состояние — восемь групп, каждая — словарь. Путь операции — ключи через точку внутри группы
(`intentions.int_ABC12345.status`); проходить можно только по словарям. Семантика:

- `set` — записать значение (недостающие словари по пути создаются);
- `add` — дописать значение в список по пути (списка нет — создаётся `[значение]`);
- `remove` — без значения удаляет ключ (он должен существовать); со значением убирает первое равное
  значение из списка;
- `transition` — смена статуса по конечному автомату (`StateMachine`); путь без автомата отклоняется.

Функции чистые: исходное состояние не меняется, при ошибке `OpError` результата нет. Для каждой
операции возвращается пара (до, после) — основа журнала причин (`state.md#versioning`).
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from iam.domain import STATE_GROUPS, Op

MISSING = object()


class OpError(Exception):
    """Операция неприменима; `code` — машинная причина, str(e) — пояснение."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class StateMachine:
    """Допустимые смены статуса по пути вида ('*', 'status') внутри группы; `*` — любой ключ.
    `needs_cause` — новый статус → типы событий, одно из которых обязано быть среди оснований перехода."""

    name: str
    group: str
    pattern: tuple[str, ...]
    table: dict[str, frozenset[str]]
    needs_cause: dict[str, frozenset[str]] = field(default_factory=dict)
    initial: frozenset[str] = frozenset()   # с каких статусов запись может появиться

    def matches(self, group: str, segs: list[str]) -> bool:
        return (group == self.group and len(segs) == len(self.pattern)
                and all(p in ("*", s) for p, s in zip(self.pattern, segs)))


def status_map(state: dict, machine: StateMachine) -> dict[tuple[str, ...], object]:
    """Все статусы, которыми управляет автомат: конкретный путь (ключи внутри группы) → значение."""
    found: dict[tuple[str, ...], object] = {}

    def walk(node, depth: int, trail: tuple[str, ...]) -> None:
        if depth == len(machine.pattern):
            found[trail] = node
        elif isinstance(node, dict):
            for key in (node if machine.pattern[depth] == "*" else [k for k in (machine.pattern[depth],) if k in node]):
                walk(node[key], depth + 1, (*trail, key))

    walk(state[machine.group], 0, ())
    return found


def machine_violations(before: dict, after: dict, moved: set[tuple[str, str, ...]],
                       machines: Iterable[StateMachine]) -> list[str]:
    """Статусы под автоматом меняются только операцией `transition`: запись создаётся с начального статуса,
    статус не правится через set/add/remove (в том числе записью целого родителя) и запись не исчезает.
    `moved` — пути (группа, ключи…), по которым в этом переходе прошла операция `transition`."""
    errs = []
    for m in machines:
        was, now = status_map(before, m), status_map(after, m)
        for path, value in now.items():
            where = ".".join((m.group, *path))
            if path not in was:
                if value not in m.initial:
                    errs.append(f"{where}: запись создаётся со статуса {sorted(m.initial)}, а не {value!r}")
            elif value != was[path] and (m.group, *path) not in moved:
                errs.append(f"{where}: статус меняется только операцией transition ({was[path]!r} → {value!r})")
        errs += [f"{'.'.join((m.group, *path))}: запись со статусом нельзя удалить" for path in was if path not in now]
    return errs


def split_path(path: str) -> list[str]:
    segs = path.split(".")
    if any(not s for s in segs):
        raise OpError("bad_path", f"путь {path!r}: пустой сегмент")
    return segs


def check_json(value: Any, where: str = "значение") -> None:
    """Значение должно быть обычным JSON: словари со строковыми ключами, списки, строки, числа
    (конечные), bool, null. Всё остальное (кортежи, множества, NaN) в состоянии недопустимо."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OpError("bad_value", f"{where}: нечисловое или бесконечное число")
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            check_json(v, f"{where}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise OpError("bad_value", f"{where}: ключ {k!r} не строка")
            check_json(v, f"{where}.{k}")
        return
    raise OpError("bad_value", f"{where}: тип {type(value).__name__} недопустим в состоянии")


def empty_state() -> dict:
    return {g: {} for g in STATE_GROUPS}


def _need_value(op: Op) -> None:
    if not op.has_value:
        raise OpError("bad_op", f"{op.op} {op.group}.{op.path}: нужно значение (value)")
    check_json(op.value, f"{op.group}.{op.path}")


def _descend(group: dict, segs: list[str], create: bool, op: Op) -> dict:
    node = group
    for i, s in enumerate(segs[:-1]):
        child = node.get(s, MISSING)
        if child is MISSING:
            if not create:
                raise OpError("path_missing", f"{op.group}.{op.path}: нет «{'.'.join(segs[:i + 1])}»")
            child = node[s] = {}
        if not isinstance(child, dict):
            raise OpError("path_blocked", f"{op.group}.{op.path}: «{'.'.join(segs[:i + 1])}» не словарь")
        node = child
    return node


def apply_op(state: dict, op: Op, machines: Iterable[StateMachine] = ()) -> dict:
    """Применить одну операцию к `state` (изменяет его на месте — копию делает вызывающий).
    Возвращает запись изменения `{group, path, op, before, after}`."""
    if op.group not in state:
        raise OpError("bad_group", f"группа {op.group!r}: не группа состояния")
    segs = split_path(op.path)
    last = segs[-1]
    where = f"{op.group}.{op.path}"
    if op.op in ("set", "add"):
        _need_value(op)
        node = _descend(state[op.group], segs, True, op)
        before = node.get(last, MISSING)
        if op.op == "set":
            node[last] = copy.deepcopy(op.value)
        elif before is MISSING:
            node[last] = [copy.deepcopy(op.value)]
        elif isinstance(before, list):
            node[last] = before + [copy.deepcopy(op.value)]
        else:
            raise OpError("not_a_list", f"add {where}: по пути не список")
        after = node[last]
    elif op.op == "remove":
        if op.has_value:
            check_json(op.value, where)
        node = _descend(state[op.group], segs, False, op)
        before = node.get(last, MISSING)
        if before is MISSING:
            raise OpError("path_missing", f"remove {where}: пути нет")
        if op.has_value:
            if not isinstance(before, list) or op.value not in before:
                raise OpError("value_missing", f"remove {where}: значения нет в списке")
            rest = list(before)
            rest.remove(op.value)
            node[last] = after = rest
        else:
            del node[last]
            after = MISSING
    elif op.op == "transition":
        _need_value(op)
        node = _descend(state[op.group], segs, False, op)
        before = node.get(last, MISSING)
        if before is MISSING:
            raise OpError("path_missing", f"transition {where}: пути нет")
        machine = next((m for m in machines if m.matches(op.group, segs)), None)
        if machine is None:
            raise OpError("no_machine", f"transition {where}: для пути нет конечного автомата")
        if not isinstance(op.value, str) or op.value not in machine.table.get(before, frozenset()):
            raise OpError("bad_transition", f"{machine.name}: {before!r} → {op.value!r} недопустимо")
        node[last] = after = op.value
    else:  # схема перехода других операций не пропускает; защита от вручную собранного Op
        raise OpError("bad_op", f"неизвестная операция {op.op!r}")
    return {"group": op.group, "path": op.path, "op": op.op,
            "before": None if before is MISSING else copy.deepcopy(before),
            "after": None if after is MISSING else copy.deepcopy(after)}


def apply_ops(state: dict, ops: Iterable[Op], machines: Iterable[StateMachine] = ()) -> tuple[dict, list[dict]]:
    """Применить операции подряд к копии `state`. Возвращает (новое состояние, изменения).
    Ошибка любой операции — `OpError`, исходное состояние не тронуто (переход атомарен)."""
    new = copy.deepcopy(state)
    machines = tuple(machines)
    return new, [apply_op(new, op, machines) for op in ops]


def touched(op: Op) -> tuple[str, ...]:
    return (op.group, *op.path.split("."))


def overlaps(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Пути затрагивают одно и то же поле: равны или один — начало другого."""
    n = min(len(a), len(b))
    return a[:n] == b[:n]
