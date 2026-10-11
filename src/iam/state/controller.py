"""StateController — проверяющее ядро: единственный писатель состояния и журнала (R-105).

Все изменения приходят как **предложения** (`Proposal`) с версией состояния, на которой они основаны.
Привратник проверяет по порядку: структуру → не применялось ли уже (ключ идемпотентности) → создана ли
личность → не устарела ли версия `(epoch, state_version)` → права исполнителя, допустимость в текущем сеансе,
защищённые поля, основания, операции и конечные автоматы, пользовательские валидаторы — и лишь затем в **одной
транзакции** пишет события перехода, записи памяти и новый снимок состояния. Любой отказ оставляет состояние
прежним и записывается в журнал (`proposal.stale` / `proposal.rejected` / `system.error`).

Модель сюда не пишет: её ответ принимает `receive_response`, который сверяет эхо запроса и версию, применяет
каждое предложение через те же проверки и объединяет принятые в один переход (E-10).
"""
from __future__ import annotations

import copy
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from iam.clock import (ClockError, LifeClock, calendar_diff_ms, due_moments, moment_errors)
from iam.domain import CATALOG, Event, MemoryRecord, Op, STATE_GROUPS, Transition, ValidationError, canonical_json, jsonschema, new_id
from iam.storage import IdempotencyConflict, NotFound, Snapshot, Store

from . import rules
from .errors import ClockRegression, NotSupported, StateCorrupt, StateError
from .paths import OpError, apply_op, empty_state, machine_violations, overlaps, split_path, touched
from .types import (ApplyResult, EventSpec, KindContext, KindDecision, Proposal, RecordResult, ResponseResult,
                    Violation)

Validator = Callable[["Check"], "Iterable[str]"]
KindHandler = Callable[[dict, KindContext], KindDecision]


@dataclass(frozen=True)
class Check:
    """Что видит пользовательский валидатор: состояние до и после, изменения, кто и каким событием меняет."""

    before: dict
    after: dict
    changes: tuple[dict, ...]
    actor: str
    carrier_type: str


@dataclass(frozen=True)
class _Stamp:
    life: int
    at: str


@dataclass
class _Outcome:
    violations: list[Violation]
    state: dict
    changes: list[dict]
    memory: list[tuple[str, MemoryRecord]]


def _ident(value) -> str:
    """Короткий отпечаток для ключей записей об ошибках (без «:»)."""
    try:
        raw = canonical_json(value)
    except TypeError:
        raw = repr(value)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _require(condition: bool, message: str) -> None:
    """Защита целостности: нарушение означает ошибку в самом привратнике, а не во входных данных."""
    if not condition:
        raise StateCorrupt(message)


class StateController:
    def __init__(self, store: Store, *, clock: LifeClock | None = None, validators: Iterable[tuple[str, Validator]] = (),
                 recover: bool = True):
        self.store = store
        self.persona_id = store.persona_id
        self.clock = clock if clock is not None else LifeClock.recover(store)
        if store.store_kind == "permanent" and self.clock.time_scale != 1:
            raise ValueError("постоянная личность живёт только с time_scale = 1 (R-165)")
        self._validators: list[tuple[str, Validator]] = list(validators)
        self._kinds: dict[str, KindHandler] = {}
        self._pending: dict[str, int] = {}
        self._reload()
        if not store.readonly:
            if self.clock.now_ms() < LifeClock.recovered_ms(store):
                raise ClockRegression("часы отстают от записанного в хранилище: время жизни не может убывать")
            if recover and self.session_state == "running":
                self._recover_crash()

    # --- чтение состояния ----------------------------------------------------------------------

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def state_version(self) -> int:
        return self._version

    @property
    def state(self) -> dict:
        """Копия текущего состояния (изменять её бессмысленно: меняет только apply)."""
        return copy.deepcopy(self._state)

    @property
    def session_state(self) -> str:
        return self._state["world_time"].get("session_state", "stopped")

    @property
    def session_id(self) -> str | None:
        return self._state["world_time"].get("session_id")

    @property
    def life_time_ms(self) -> int:
        return self.clock.now_ms()

    def add_validator(self, name: str, fn: Validator) -> None:
        """Подключить проверку группы (диапазоны, происхождение…): возвращает список текстов нарушений."""
        self._validators.append((name, fn))

    def register_kind(self, kind: str, handler: KindHandler) -> None:
        """Подключить разборщик вида предложения (`action`, `message`, …): превращает предложение в операции
        состояния или отклоняет его. Без разборщика вид отклоняется как `kind_not_supported`; `state_change`
        разбирает сам привратник."""
        if kind == "state_change":
            raise ValueError("state_change разбирает сам привратник")
        self._kinds[kind] = handler

    # --- чтение журнала (для цикла: сам он в хранилище не заходит) -----------------------------

    def find_event(self, *, event_id: str | None = None, key: str | None = None) -> Event | None:
        return self.store.find_event(event_id=event_id, key=key)

    def events_after(self, seq: int) -> list[Event]:
        """События журнала с номером больше `seq`, по порядку."""
        return list(self.store.events(seq + 1))

    def _reload(self) -> None:
        snap = self.store.latest_snapshot()
        if snap is None:
            self._epoch, self._version, self._state, at_seq = 1, 0, empty_state(), 0
        else:
            if set(snap.state) != set(STATE_GROUPS):
                raise StateCorrupt(f"снимок ({snap.epoch}, {snap.state_version}): группы не те, что в спецификации")
            self._epoch, self._version, self._state, at_seq = snap.epoch, snap.state_version, snap.state, snap.at_seq
        orphan = self.store.con.execute("SELECT seq FROM events WHERE seq > ? AND changes_state = 1 LIMIT 1",
                                        (at_seq,)).fetchone()
        if orphan is not None:
            raise StateCorrupt(f"событие seq={orphan[0]} меняет состояние, но снимка после него нет")
        last = self.store.last_seq()
        self._last_life = self.store.get_event(last).life_time_ms if last else 0

    @contextmanager
    def _atomic(self):
        """Транзакция хранилища; при любом исключении состояние в памяти перечитывается из базы."""
        if self.store.readonly:
            raise StateError("хранилище открыто только для чтения")
        try:
            with self.store.transaction():
                yield
        except BaseException:
            self._reload()
            raise

    # --- штамп времени и запись событий --------------------------------------------------------

    def _stamp(self, life: int | None = None) -> _Stamp:
        life = self.clock.now_ms() if life is None else life
        if life < self._last_life:
            raise ClockRegression(f"время жизни {life} меньше уже записанного {self._last_life}")
        return _Stamp(life, self.clock.calendar_now())

    def _event(self, spec: EventSpec, actor: str, actor_ref: str, stamp: _Stamp, *, version: int, changes_state: bool,
               fallback_causes: tuple[str, ...] = ()) -> Event:
        return Event(event_id=spec.event_id or new_id("evt"), persona_id=self.persona_id, type=spec.type,
                     occurred_at=stamp.at, life_time_ms=stamp.life, source=rules.event_source(actor, actor_ref),
                     idempotency_key=spec.idempotency_key, epoch=self._epoch, state_version=version,
                     changes_state=changes_state, payload=spec.payload, causes=spec.causes or fallback_causes)

    def _log(self, spec: EventSpec, actor: str = "core", actor_ref: str = "state_controller",
             causes: tuple[str, ...] = ()) -> Event:
        """Записать событие, не меняющее состояние, если такого ключа ещё нет (повтор возвращает исходное)."""
        have = self.store.find_event(key=spec.idempotency_key)
        if have is not None:
            return have
        ev = self._event(spec, actor, actor_ref, self._stamp(), version=self._version, changes_state=False,
                         fallback_causes=causes)
        return self.store.append_event(ev)[0]

    def _fault(self, fault: str, ident: str, payload: dict) -> Event:
        return self._log(EventSpec("system.error", f"err:{fault}:{ident}", {"fault": fault, **payload}))

    # --- запись событий, не меняющих состояние -------------------------------------------------

    def record_event(self, spec: EventSpec, *, actor: str = "core", actor_ref: str = "state_controller") -> RecordResult:
        """Дописать событие с `changes_state = false` (поступление сообщения, запросы, ошибки…).
        Повтор с тем же ключом и содержимым возвращает исходное событие, с другим содержимым — конфликт
        (оригинал не перезаписывается, пишется `system.error`)."""
        kind = CATALOG.get(spec.type)
        if kind is None or kind.changes_state:
            raise ValueError(f"{spec.type!r}: не событие без изменения состояния (меняющие состояние идут через apply)")
        if spec.type in ("proposal.requested", "proposal.received"):
            raise ValueError(f"{spec.type}: запись только через record_request / receive_response")
        return self._record(spec, actor, actor_ref)

    def _record(self, spec: EventSpec, actor: str = "core", actor_ref: str = "state_controller") -> RecordResult:
        with self._atomic():
            if not rules.permitted_in(self.session_state, spec.type):
                return self._refuse(spec, "session_forbids",
                                    f"{spec.type} недопустимо при сеансе {self.session_state}")
            ev = self._event(spec, actor, actor_ref, self._stamp(), version=self._version, changes_state=False)
            errs = ev.errors()
            if errs:
                return self._refuse(spec, "invalid_event", "; ".join(errs))
            try:
                stored, created = self.store.append_event(ev)
            except IdempotencyConflict as e:
                self._fault("idempotency_conflict", _ident([spec.idempotency_key, spec.payload]),
                            {"key": e.key, "existing_seq": e.existing_seq})
                return RecordResult("conflict", reason="idempotency_conflict")
            return RecordResult("recorded" if created else "duplicate", stored)

    def _refuse(self, spec: EventSpec, code: str, message: str) -> RecordResult:
        self._fault("rejected", _ident([spec.type, spec.idempotency_key, code]),
                    {"type": spec.type, "key": spec.idempotency_key, "code": code, "message": message})
        return RecordResult("rejected", reason=code, violations=(Violation(code, message),))

    # --- применение перехода -------------------------------------------------------------------

    def apply(self, proposal: Proposal) -> ApplyResult:
        """Проверить и применить предложение перехода (см. модуль). Поля сеанса и времени этим путём
        менять нельзя — только методами управления сеансом."""
        return self._apply(proposal, privileged=False)

    def _apply(self, p: Proposal, *, privileged: bool, internal: bool | None = None,
               life: int | None = None) -> ApplyResult:
        """`privileged` — можно писать защищённые поля сеанса; `internal` (по умолчанию как privileged) — можно
        проводить зарезервированные события (вызывают только методы самого контроллера)."""
        with self._atomic():
            t = p.transition
            carrier = p.carrier or EventSpec("state.transition_applied", f"trn:{t.transition_id}")
            carrier = carrier if carrier.event_id else replace(carrier, event_id=new_id("evt"))
            early = self._structural(p, carrier)
            if early:
                return self._reject(p, early, structural=True)
            have = self.store.find_event(key=carrier.idempotency_key)
            if have is not None:
                if canonical_json(have.payload.get("transition")) == canonical_json(t.to_dict()):
                    return ApplyResult("duplicate", self._epoch, self._version, events=(have,),
                                       changes=tuple(have.payload["changes"]))
                self._fault("idempotency_conflict", _ident([carrier.idempotency_key, t.to_dict()]),
                            {"key": carrier.idempotency_key, "existing_seq": have.seq,
                             "transition_id": t.transition_id})
                return ApplyResult("conflict", self._epoch, self._version, "idempotency_conflict")
            created = carrier.type == "persona.created"
            if created == (self._version > 0):
                return self._reject(p, [Violation("persona_exists" if created else "persona_not_created",
                                                  "личность уже создана" if created else "личность ещё не создана")])
            if (t.base_epoch, t.base_version) != (self._epoch, self._version):
                return self._stale(p)
            stamp = self._stamp(life)
            out = self._check(p, carrier, privileged, privileged if internal is None else internal, stamp)
            if out.violations:
                return self._reject(p, out.violations)
            return self._commit(p, carrier, out, stamp)

    def _structural(self, p: Proposal, carrier: EventSpec) -> list[Violation]:
        t = p.transition
        errs = jsonschema.errors("transition", t.to_dict())
        if errs:
            return [Violation("invalid_transition", e) for e in errs]
        kind = CATALOG.get(carrier.type)
        if kind is None or not kind.changes_state:
            return [Violation("bad_carrier", f"{carrier.type!r} не может нести переход состояния")]
        found = []
        if t.persona_id != self.persona_id:
            found.append(Violation("wrong_persona", f"переход личности {t.persona_id}, а это {self.persona_id}"))
        if {"transition", "changes"} & set(carrier.payload):
            found.append(Violation("payload_reserved", "поля transition и changes заполняет ядро"))
        return found

    def _reject(self, p: Proposal, violations: Iterable[Violation], *, structural: bool = False) -> ApplyResult:
        t, violations = p.transition, tuple(violations)
        payload = {"transition_id": t.transition_id, "reason": violations[0].code,
                   "violations": [v.to_dict() for v in violations]}
        if t.proposal_id and not structural:
            self._log(EventSpec("proposal.rejected", f"prop:{t.proposal_id}:reject",
                                {"proposal_id": t.proposal_id, **payload}), p.actor, p.actor_ref)
        else:
            self._fault("rejected", _ident(t.transition_id), payload)
        return ApplyResult("rejected", self._epoch, self._version, violations[0].code, violations)

    def _stale(self, p: Proposal) -> ApplyResult:
        t = p.transition
        self._log(EventSpec("proposal.stale", f"resp:{p.request_id or t.transition_id}:stale", {
            "request_id": p.request_id, "transition_id": t.transition_id, "base_epoch": t.base_epoch,
            "base_version": t.base_version, "current_epoch": self._epoch, "current_version": self._version}),
            p.actor, p.actor_ref)
        return ApplyResult("stale", self._epoch, self._version, "stale_version")

    # --- проверка содержимого перехода ---------------------------------------------------------

    def _check(self, p: Proposal, carrier: EventSpec, privileged: bool, internal: bool, stamp: _Stamp) -> _Outcome:
        t = p.transition
        v: list[Violation] = []
        if p.actor not in rules.CARRIER_ACTORS.get(carrier.type, rules.DEFAULT_CARRIER_ACTORS):
            v.append(Violation("actor_forbidden", f"{p.actor} не вправе проводить событие {carrier.type}"))
        for spec in p.companions:
            if p.actor not in rules.CARRIER_ACTORS.get(spec.type, rules.DEFAULT_CARRIER_ACTORS):
                v.append(Violation("actor_forbidden", f"{p.actor} не вправе проводить событие {spec.type}"))
        if not internal and {carrier.type, *(s.type for s in p.companions)} & rules.RESERVED_CARRIERS:
            v.append(Violation("carrier_reserved", "события сеанса, моментов и создания личности пишут методы контроллера"))
        for spec in (carrier, *p.companions):
            if not rules.permitted_in(self.session_state, spec.type):
                v.append(Violation("session_forbids", f"{spec.type} недопустимо при сеансе {self.session_state}"))
        if not privileged:
            v += self._protected(t.ops)
        out = self._run_ops(t.ops, p.actor, carrier.type, self._state, t.caused_by, carrier.event_id)
        out.violations[:0] = v
        if not out.violations:
            events = self._events(p, carrier, self._version + 1, stamp)
            for ev in events:
                out.violations += [Violation("invalid_event", e) for e in ev.errors()]
            out.violations += [Violation("key_taken", f"ключ {s.idempotency_key} уже занят")
                               for s in p.companions if self.store.find_event(key=s.idempotency_key)]
        return out

    @staticmethod
    def _protected(ops: Iterable[Op]) -> list[Violation]:
        return [Violation("protected_field", f"{o.group}.{o.path}: поле принадлежит контроллеру сеанса")
                for o in ops if o.group == "world_time" and o.path.split(".")[0] in rules.PROTECTED_WORLD_TIME]

    def _run_ops(self, ops: Iterable[Op], actor: str, carrier_type: str, base: dict, caused_by: Iterable[str],
                 own_id: str | None) -> _Outcome:
        """Применить операции к копии `base` и прогнать проверки; ничего не записывает."""
        v: list[Violation] = []
        causes: set[str] = set()
        for c in caused_by:
            if c == own_id and actor in ("operator", "core"):
                continue  # корневое событие: оператор или ядро — сами себе основание
            found = self.store.find_event(event_id=c)
            if found is None:
                v.append(Violation("unknown_cause", f"основание {c} не найдено в журнале"))
            else:
                causes.add(found.type)
        state, changes, memory, overlay = copy.deepcopy(base), [], [], {}
        for op in ops:
            try:
                if op.group == "memory":
                    if actor == "model":
                        raise OpError("memory_via_proposal", "память модель меняет предложением memory_write, а не операцией")
                    changes.append(self._memory_op(op, overlay, memory))
                else:
                    changes.append(apply_op(state, op, rules.MACHINES))
                    machine = next((m for m in rules.MACHINES if op.op == "transition"
                                    and m.matches(op.group, split_path(op.path))), None)
                    need = machine.needs_cause.get(op.value) if machine else None
                    if need and not need & causes:
                        v.append(Violation("needs_confirmation", f"{op.group}.{op.path} → {op.value!r}: нужно "
                                           f"основание-подтверждение ({', '.join(sorted(need))})"))
            except (OpError, ValidationError) as e:
                v.append(Violation(getattr(e, "code", "bad_memory"), str(e)))
        out = _Outcome(v, state, changes, memory)
        if not v:
            moved = {(o.group, *o.path.split(".")) for o in ops if o.op == "transition"}
            out.violations += [Violation("machine_bypass", m) for m in machine_violations(base, state, moved, rules.MACHINES)]
        if not out.violations:
            for name, fn in self._validators:
                out.violations += [Violation(f"validator:{name}", msg) for msg in fn(
                    Check(copy.deepcopy(base), copy.deepcopy(state), copy.deepcopy(tuple(changes)), actor, carrier_type))]
        return out

    def _memory_op(self, op: Op, overlay: dict, writes: list) -> dict:
        segs = split_path(op.path)
        if len(segs) != 1 or op.op not in ("add", "set") or not op.has_value:
            raise OpError("memory_op_unsupported", f"memory {op.op} {op.path}: пока поддержаны add/set записи по memory_id "
                          "(слияние, забывание, архив — срезы S18–S19)")
        rec = MemoryRecord.from_dict(op.value)
        if rec.memory_id != op.path:
            raise OpError("memory_id_mismatch", f"путь {op.path} и memory_id {rec.memory_id} не совпадают")
        if rec.persona_id != self.persona_id:
            raise OpError("wrong_persona", f"запись памяти другой личности {rec.persona_id}")
        if rec.layer == "archive":
            raise OpError("archive_write", "слой archive обычным переходом не пишется (вмешательство оператора, S06/S18)")
        if rec.origin == "simulation_event" and self.store.store_kind == "permanent":
            raise OpError("simulation_in_permanent", "события симуляции не попадают в постоянное хранилище")
        old = overlay.get(rec.memory_id)
        if old is None:
            try:
                old = self.store.get_memory(rec.memory_id)
            except NotFound:
                old = None
        if op.op == "add":
            if old is not None:
                raise OpError("memory_exists", f"запись {rec.memory_id} уже есть")
        elif old is None:
            raise OpError("memory_missing", f"set: записи {rec.memory_id} нет")
        elif rec.layer != old.layer or rec.version < old.version:
            raise OpError("memory_immutable", f"{rec.memory_id}: слой неизменяем, версия не убывает")
        overlay[rec.memory_id] = rec
        writes.append(("add" if op.op == "add" else "update", rec))
        return {"group": "memory", "path": op.path, "op": op.op, "before": old and old.to_dict(), "after": rec.to_dict()}

    # --- фиксация перехода ---------------------------------------------------------------------

    def _events(self, p: Proposal, carrier: EventSpec, version: int, stamp: _Stamp,
                changes: Iterable[dict] = ()) -> list[Event]:
        t = p.transition
        own = carrier.event_id
        causes = tuple(c for c in t.caused_by if c != own)
        body = {"transition": t.to_dict(), "changes": list(changes), **carrier.payload}
        head = EventSpec(carrier.type, carrier.idempotency_key, body, carrier.causes, own)
        return [self._event(s, p.actor, p.actor_ref, stamp, version=version, changes_state=True, fallback_causes=causes)
                for s in (head, *p.companions)]

    def _commit(self, p: Proposal, carrier: EventSpec, out: _Outcome, stamp: _Stamp) -> ApplyResult:
        version = self._version + 1
        written = []
        for ev in self._events(p, carrier, version, stamp, out.changes):
            stored, created = self.store.append_event(ev)
            _require(created, f"ключ {ev.idempotency_key} занят после проверки: событие перехода не записано")
            written.append(stored)
        for how, rec in out.memory:
            (self.store.add_memory if how == "add" else self.store.update_memory)(rec)
        self.store.put_snapshot(Snapshot(self._epoch, version, written[-1].seq, stamp.life, stamp.at, out.state))
        self._state, self._version, self._last_life = out.state, version, stamp.life
        return ApplyResult("applied", self._epoch, version, events=tuple(written), changes=tuple(out.changes))

    # --- личность ------------------------------------------------------------------------------

    def create_persona(self, initial: dict[str, dict] | None = None, *, reason: str = "создание личности",
                       actor_ref: str = "panel") -> ApplyResult:
        """Создать личность событием `persona.created`: начальные значения групп (`initial`: группа → ключи)
        плюс стандартные поля `world_time`. Идентификаторы выводятся из `persona_id`, поэтому повтор того же
        вызова — `duplicate`, а другого содержимого — `conflict`."""
        initial = initial or {}
        defaults = copy.deepcopy(rules.WORLD_TIME_DEFAULT)
        wt = {**defaults, "world": {**defaults["world"], **initial.get("world_time", {}).get("world", {})}}
        spare = set(initial.get("world_time", {})) - {"world"}
        if spare:
            raise ValueError(f"world_time: поля {sorted(spare)} задаёт контроллер сеанса")
        groups = {**initial, "world_time": wt}
        ops = tuple(Op(g, k, "set", v, True) for g, body in groups.items() for k, v in body.items())
        tail = self.persona_id.split("_", 1)[1]
        t = Transition(f"trn_{tail}", self.persona_id, 1, 0, ops, reason, (f"evt_{tail}",))
        carrier = EventSpec("persona.created", f"per:{self.persona_id}:create", {"groups": sorted(groups)},
                            event_id=f"evt_{tail}")
        return self._apply(Proposal(t, "operator", actor_ref, carrier), privileged=True)

    # --- сеанс и время жизни -------------------------------------------------------------------

    def _refuse_call(self, action: str, code: str, message: str) -> ApplyResult:
        """Отказ ещё до построения перехода (неверное состояние сеанса и т. п.) — с записью в журнал."""
        with self._atomic():
            self._fault("rejected", _ident([action, code, self._epoch, self._version, message]),
                        {"action": action, "code": code, "message": message})
        return ApplyResult("rejected", self._epoch, self._version, code, (Violation(code, message),))

    def _expect_session(self, action: str, allowed: tuple[str, ...]) -> ApplyResult | None:
        if self.session_state in allowed:
            return None
        return self._refuse_call(action, "bad_session_state",
                                 f"{action}: сеанс {self.session_state}, а нужен один из {allowed}")

    def _session_step(self, event_type: str, key: str, ops: list[Op], payload: dict, life: int, actor: str,
                      why: str) -> ApplyResult:
        ev_id = new_id("evt")
        t = Transition(new_id("trn"), self.persona_id, self._epoch, self._version, tuple(ops), why, (ev_id,))
        carrier = EventSpec(event_type, key, payload, event_id=ev_id)
        return self._apply(Proposal(t, actor, "session", carrier), privileged=True, life=life)

    @staticmethod
    def _wt(path: str, value) -> Op:
        return Op("world_time", path, "set", value, True)

    def start_session(self, *, session_id: str | None = None) -> ApplyResult:
        """Начать сеанс: время жизни начинает идти с сохранённого значения."""
        bad = self._expect_session("start", ("stopped",))
        if bad:
            return bad
        sid, life = session_id or new_id("ses"), self.clock.now_ms()
        if self.store.find_event(key=f"ses:{sid}:start") is not None:
            return self._refuse_call("start", "session_id_used", f"сеанс {sid} уже был: идентификатор не повторяется")
        ops = [Op("world_time", "session_state", "transition", "running", True), self._wt("session_id", sid),
               self._wt("life_time_ms", life), self._wt("pause_reason", None), self._wt("paused_at", None),
               self._wt("pause_count", 0), self._wt("resume_count", 0)]
        res = self._session_step("session.started", f"ses:{sid}:start", ops, {"session_id": sid, "life_time_ms": life},
                                 life, "operator", "начало сеанса")
        if res.applied:
            self.clock.resume()
        return res

    def pause_session(self, reason: str = "operator", *, last_alive_at: str | None = None) -> ApplyResult:
        """Поставить сеанс на паузу: время жизни замирает ровно на записанном в событии значении."""
        if reason not in rules.PAUSE_REASONS:
            raise ValueError(f"причина паузы {reason!r}: нужна одна из {rules.PAUSE_REASONS}")
        bad = self._expect_session("pause", ("running",))
        if bad:
            return bad
        wt, life = self._state["world_time"], self.clock.now_ms()
        sid, n = wt["session_id"], wt["pause_count"] + 1
        ops = [Op("world_time", "session_state", "transition", "paused", True), self._wt("life_time_ms", life),
               self._wt("pause_reason", reason), self._wt("paused_at", last_alive_at or self.clock.calendar_now()),
               self._wt("pause_count", n)]
        res = self._session_step("session.paused", f"ses:{sid}:pause:{n}", ops,
                                 {"session_id": sid, "reason": reason, "pause_n": n, "life_time_ms": life}, life,
                                 "operator" if reason == "operator" else "core", f"пауза сеанса: {reason}")
        if res.applied and self.clock.running:
            self.clock.freeze(life)
        return res

    def resume_session(self) -> ApplyResult:
        """Возобновить сеанс: время жизни продолжается с прежнего значения; сколько календарного времени
        прошло в паузе — записывается как данные (`paused_calendar_ms`), не как прожитое."""
        bad = self._expect_session("resume", ("paused",))
        if bad:
            return bad
        wt, life = self._state["world_time"], self.clock.now_ms()
        sid, n = wt["session_id"], wt["resume_count"] + 1
        gap = max(0, calendar_diff_ms(self.clock.calendar_now(), wt["paused_at"]))
        ops = [Op("world_time", "session_state", "transition", "running", True), self._wt("life_time_ms", life),
               self._wt("pause_reason", None), self._wt("paused_at", None), self._wt("resume_count", n)]
        res = self._session_step("session.resumed", f"ses:{sid}:resume:{n}", ops,
                                 {"session_id": sid, "paused_calendar_ms": gap, "resume_n": n, "life_time_ms": life,
                                  "was_paused_for": wt["pause_reason"]}, life, "operator", "возобновление сеанса")
        if res.applied:
            self.clock.resume()
        return res

    def stop_session(self) -> ApplyResult:
        """Остановить сеанс (из работы или паузы)."""
        bad = self._expect_session("stop", ("running", "paused"))
        if bad:
            return bad
        sid, life = self.session_id, self.clock.now_ms()
        ops = [Op("world_time", "session_state", "transition", "stopped", True), self._wt("session_id", None),
               self._wt("life_time_ms", life), self._wt("pause_reason", None), self._wt("paused_at", None)]
        res = self._session_step("session.stopped", f"ses:{sid}:stop", ops, {"session_id": sid, "life_time_ms": life},
                                 life, "operator", "остановка сеанса")
        if res.applied and self.clock.running:
            self.clock.freeze(life)
        return res

    def _recover_crash(self) -> ApplyResult:
        """Процесс запускается, а в состоянии сеанс `running`: прошлый запуск оборвался. Пишем паузу `crash`;
        время жизни — максимум из контрольной точки и последнего события (прошедшее после не засчитывается)."""
        return self.pause_session("crash", last_alive_at=self.store.clock()[1])

    # --- плановые моменты ----------------------------------------------------------------------

    def schedule_moment(self, moment: dict) -> ApplyResult:
        errs = moment_errors(moment)
        if errs:
            return self._refuse_call("schedule_moment", "bad_moment", "; ".join(errs))
        if (moment["moment_id"] in self._state["world_time"]["moments"]
                or self.store.find_event(key=f"due:{moment['moment_id']}:1") is not None):
            return self._refuse_call("schedule_moment", "moment_exists",
                                     f"момент {moment['moment_id']} уже назначен или уже наступал: id не повторяется")
        return self._moment_step(Op("world_time", f"moments.{moment['moment_id']}", "set", moment, True),
                                 f"назначен момент {moment['moment_id']}")

    def cancel_moment(self, moment_id: str) -> ApplyResult:
        return self._moment_step(Op("world_time", f"moments.{moment_id}", "remove"), f"отменён момент {moment_id}")

    def _moment_step(self, op: Op, why: str, carrier: EventSpec | None = None) -> ApplyResult:
        ev_id = carrier.event_id if carrier else new_id("evt")
        t = Transition(new_id("trn"), self.persona_id, self._epoch, self._version, (op,), why, (ev_id,))
        carrier = carrier or EventSpec("state.transition_applied", f"trn:{t.transition_id}", event_id=ev_id)
        return self._apply(Proposal(t, "core", "clock", carrier), privileged=True)

    def fire_due_moments(self) -> list[ApplyResult]:
        """Наступившие моменты → события `time.moment_due` (по одному переходу на момент). Только пока сеанс
        идёт: на паузе время жизни стоит, а календарные сроки сработают один раз после возобновления
        (с `overdue_ms`). Фонового опроса нет — вызывает цикл/`tick`."""
        if self.session_state != "running":
            return []
        results = []
        for d in due_moments(self._state["world_time"]["moments"], self.clock.now_ms(), self.clock.calendar_now()):
            m, ev_id = d.moment, new_id("evt")
            due = {k: m[k] for k in ("due_life_ms", "due_calendar") if k in m}
            spec = EventSpec("time.moment_due", f"due:{m['moment_id']}:{d.n}",
                             {"moment_id": m["moment_id"], "kind": m["kind"], "owner_ref": m["owner_ref"], "n": d.n,
                              "overdue_ms": d.overdue_ms, **due}, event_id=ev_id)
            results.append(self._moment_step(Op("world_time", f"moments.{m['moment_id']}", "remove"),
                                             f"наступил момент {m['moment_id']}", spec))
        return results

    def tick(self) -> list[ApplyResult]:
        """Шаг обслуживания между шагами цикла: контрольная точка времени жизни и наступившие моменты."""
        self.clock.heartbeat(self.store)
        return self.fire_due_moments()

    # --- вмешательства оператора ---------------------------------------------------------------

    def intervene(self, kind: str, reason: str, ops: Iterable[Op], *, target: str = "state") -> ApplyResult:
        """Техническое вмешательство оператора (`operator.intervention`, permissions.md#interventions): только
        из панели, только с причиной (I-2), журнал не переписывается (I-3), поля сеанса — только методами сеанса.
        Работает и на паузе. Поддержан вид `technical_fix`; остальные виды — в срезах, где живут их данные."""
        if kind != "technical_fix":
            raise NotSupported(f"вмешательство {kind!r}: права и копии — S06/S23, настройки — S10")
        iid, ev_id = new_id("opr"), new_id("evt")
        t = Transition(new_id("trn"), self.persona_id, self._epoch, self._version, tuple(ops), reason.strip(), (ev_id,))
        carrier = EventSpec("operator.intervention", f"opr:{iid}", {
            "intervention_id": iid, "kind": kind, "operator_channel": "panel", "reason": reason.strip(),
            "target": target, "visible_to_persona": True}, event_id=ev_id)
        return self._apply(Proposal(t, "operator", "panel", carrier), privileged=False, internal=True)

    # --- запросы и ответы модели ---------------------------------------------------------------

    def record_request(self, request: dict) -> RecordResult:
        """Записать запрос к модели (`proposal.requested`) **до** отправки (E-3). В паузе и вне сеанса запрос
        не записывается: модель в паузе не вызывается."""
        errs = jsonschema.errors("request", request)
        if errs:
            return self._refuse(EventSpec("proposal.requested", f"req:bad{_ident(request)}"), "bad_request", "; ".join(errs))
        spec = EventSpec("proposal.requested", request["idempotency_key"], {"request": request})
        if request["persona_id"] != self.persona_id or request["session_id"] != self.session_id:
            return self._refuse(spec, "wrong_context", "запрос другой личности или не текущего сеанса")
        res = self._record(spec)
        if res.event is not None:
            self._pending.setdefault(request["request_id"], self.clock.monotonic_ms())
        return res

    def receive_response(self, response: dict) -> ResponseResult:
        """Принять ответ модели (docs/spec/envelopes.md#staleness): формат и эхо → известный запрос → повтор
        → запись `proposal.received` → версия, сеанс, срок → каждое предложение проверяется ядром; принятые
        применяются одним переходом. Устаревший, повторный и неверный ответ состояние не меняют."""
        with self._atomic():
            return self._receive(response)

    def _receive(self, response: dict) -> ResponseResult:
        errs = jsonschema.errors("response", response)
        if errs:
            self._fault("bad_response", _ident(response), {"errors": errs})
            return ResponseResult("rejected", "bad_format")
        rid = response["request_id"]
        asked = self.store.find_event(key=f"req:{rid}")
        if asked is None:
            self._fault("unknown_request", _ident(rid), {"request_id": rid})
            return ResponseResult("rejected", "unknown_request")
        body, key = {"response": response}, f"resp:{rid}"
        have = self.store.find_event(key=key)
        if have is not None:
            if canonical_json(have.payload) == canonical_json(body):
                return ResponseResult("duplicate", received=have)
            self._fault("response_conflict", _ident(body), {"request_id": rid, "existing_seq": have.seq})
            return ResponseResult("conflict", "response_conflict")
        received = self._log(EventSpec("proposal.received", key, body), causes=(asked.event_id,))
        request = asked.payload["request"]
        if (response["epoch"], response["state_version"]) != (request["epoch"], request["state_version"]):
            self._fault("echo_mismatch", _ident(rid), {"request_id": rid})
            return ResponseResult("rejected", "echo_mismatch", received)
        why = self._stale_reasons(response, request, self._pending.pop(rid, None))
        if why:
            self._log(EventSpec("proposal.stale", f"{key}:stale", {
                "request_id": rid, "reasons": why, "response_epoch": response["epoch"],
                "response_version": response["state_version"], "current_epoch": self._epoch,
                "current_version": self._version}), causes=(received.event_id,))
            return ResponseResult("stale", why[0], received)
        if response["status"] != "ok":
            return ResponseResult("ignored", response["status"], received)
        if response["tool_use_detected"]:
            self._fault("tool_use", _ident(rid), {"request_id": rid})
            rejected = {pr["proposal_id"]: "tool_use_detected" for pr in response["proposals"]}
            self._log_rejections(response, rejected, received)
            return ResponseResult("rejected", "tool_use_detected", received, rejected=rejected)
        return self._judge_proposals(response, received, request)

    def _stale_reasons(self, response: dict, request: dict, sent_at: int | None) -> list[str]:
        why = []
        if (response["epoch"], response["state_version"]) != (self._epoch, self._version):
            why.append("version")
        if self.session_state != "running" or request["session_id"] != self.session_id:
            why.append("session")
        if sent_at is None:
            why.append("deadline_unknown")
        elif self.clock.monotonic_ms() - sent_at > request["limits"]["deadline_ms"]:
            why.append("deadline")
        return why

    def _cite_exists(self, ref: str) -> bool:
        if ref.startswith("evt_"):
            return self.store.find_event(event_id=ref) is not None
        try:
            self.store.get_memory(ref)
        except NotFound:
            return False
        return True

    def _log_rejections(self, response: dict, rejected: dict, received: Event) -> None:
        for pr in response["proposals"]:
            if pr["proposal_id"] in rejected:
                self._log(EventSpec("proposal.rejected", f"prop:{pr['proposal_id']}:reject", {
                    "proposal_id": pr["proposal_id"], "request_id": response["request_id"], "kind": pr["kind"],
                    "reason": rejected[pr["proposal_id"]]}), causes=(received.event_id,))

    def _judge_proposals(self, response: dict, received: Event, request: dict) -> ResponseResult:
        """Предложения по порядку: поддержанные проверяются на копии «рабочего» состояния, конфликтующие с уже
        принятыми и нарушающие правила отклоняются поодиночке; принятые — один переход (E-10)."""
        rid, working, claimed = response["request_id"], self._state, []
        accepted, rejected, ops = [], {}, []
        ids = [pr["proposal_id"] for pr in response["proposals"]]
        if len(ids) != len(set(ids)):
            self._fault("duplicate_proposal_id", _ident(rid), {"request_id": rid})
            return ResponseResult("rejected", "duplicate_proposal_id", received)
        for pr in response["proposals"]:
            bad, working_after, new_ops = self._judge(pr, working, claimed, received.event_id, request,
                                                      tuple(p["kind"] for p in accepted))
            if bad:
                rejected[pr["proposal_id"]] = bad.code
                continue
            working = working_after
            claimed += [touched(o) for o in new_ops]
            accepted.append(pr)
            ops += new_ops
        self._log_rejections(response, rejected, received)
        if not accepted:
            return ResponseResult("rejected", "no_proposal_accepted", received, rejected=rejected)
        causes = dict.fromkeys(c for pr in accepted for c in pr["cites"] if c.startswith("evt_"))
        t = Transition(new_id("trn"), self.persona_id, self._epoch, self._version, tuple(ops),
                       f"ответ модели {rid}", (*causes, received.event_id),
                       proposal_id=accepted[0]["proposal_id"] if len(accepted) == 1 else None)
        mark = tuple(EventSpec("proposal.accepted", f"prop:{pr['proposal_id']}:accept", {
            "proposal_id": pr["proposal_id"], "request_id": rid, "kind": pr["kind"], "rationale": pr["rationale"]})
            for pr in accepted)
        res = self._apply(Proposal(t, "model", response["provider"]["name"], companions=mark, request_id=rid),
                          privileged=False)
        ok = res.applied
        names = tuple(pr["proposal_id"] for pr in accepted)
        if not ok:   # принятые по отдельности, но не записанные вместе: каждому — запись об отказе (E-9)
            rejected = {**rejected, **{n: res.reason for n in names}}
            self._log_rejections(response, rejected, received)
        status = ("partial" if rejected else "applied") if ok else "rejected"
        return ResponseResult(status, None if ok else res.reason, received, names if ok else (), rejected, res)

    def _judge(self, pr: dict, working: dict, claimed: list, received_id: str, request: dict, accepted_kinds: tuple):
        """(нарушение | None, состояние после, операции) для одного предложения."""
        if pr["kind"] == "state_change":
            ops = tuple(Op.from_dict(o) for o in pr["body"]["ops"])
        else:
            handler = self._kinds.get(pr["kind"])
            if handler is None:
                return Violation("kind_not_supported", f"предложения вида {pr['kind']} не подключены"), working, ()
            decision = handler(pr, KindContext(request, working, accepted_kinds, self.clock.now_ms()))
            if decision.violation is not None:
                return decision.violation, working, ()
            ops = tuple(decision.ops)
        if pr["kind"] not in request["allowed_proposals"]:
            return Violation("kind_not_allowed", f"вид {pr['kind']} не разрешён этим запросом"), working, ()
        if any(overlaps(touched(o), c) for o in ops for c in claimed):
            return Violation("conflicts_with_accepted", "то же поле уже меняет принятое предложение"), working, ()
        lost = [c for c in pr["cites"] if not self._cite_exists(c)]
        if lost:
            return Violation("unknown_cite", f"ссылки не найдены: {lost}"), working, ()
        bad = self._protected(ops)
        if bad:
            return bad[0], working, ()
        cites = tuple(c for c in pr["cites"] if c.startswith("evt_"))
        out = self._run_ops(ops, "model", "state.transition_applied", working, (*cites, received_id), None)
        return (out.violations[0] if out.violations else None), out.state, ops
