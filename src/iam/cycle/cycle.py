"""Исполнитель общего цикла (PLAN §2.8, R-103):

**событие → внимание → извлечение памяти → оценка → предложение → проверка ядром → действие →
подтверждённый результат → обновление памяти.**

Цикл запускается событием, а не таймером: нет события — нет обращения к модели (R-036). Модель только
предлагает (R-104); состояние и память меняет привратник. Каждая стадия оставляет в журнале события с
идентификаторами и версиями, а в отчёте — запись `StageRecord`. Действие объявляется выполненным только по
событию подтверждающего источника; сбой или потерянный ответ — «исход неизвестен», а не успех
(R-184, R-199, R-222).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable

from iam.domain import Event, MemoryRecord, Op, Source, Transition, new_id
from iam.interfaces import (ActionOutcome, ActionRequest, Capabilities, CognitiveProvider, CreatorBridge,
                            CreatorRequest, Delivery, Memory, StateController, World)
from iam.state import ApplyResult, EventSpec, Proposal

from .attention import AttentionPolicy, Focus
from .handlers import EXECUTABLE, action_id_for, make_kind_handlers
from .validator import tool_use_signs

STAGES = ("event", "attention", "retrieval", "appraisal", "proposal", "verification", "action", "confirmation",
          "memory_update")
TRIGGERS = ("time.moment_due", "input.arrived", "activity.completed")
DEFAULT_ALLOWED = ("action", "message", "creator_request", "no_action", "attend")
READABLE_LAYERS = ("personal", "interpretation")

# Типы событий и ключи по виду действия (events.md#event-types): попытка / исход.
_ACTION_EVENTS = {
    "action": {"attempt": ("action.attempted", "act:{}:try"), "confirmed": ("action.confirmed", "act:{}:ok"),
               "failed": ("action.failed", "act:{}:fail"), "unknown": ("action.unknown_outcome", "act:{}:unk")},
    "message": {"attempt": ("message.out_requested", "out:{}"), "confirmed": ("message.out_sent", "out:{}:ok"),
                "failed": ("action.failed", "act:{}:fail"), "unknown": ("message.out_unknown", "out:{}:unk")},
}
_RESULT_STATUS = {"confirmed": "confirmed", "failed": "failed", "unknown": "unknown_outcome"}


def cycle_id(trigger_id: str, round_: int = 0) -> str:
    """Идентификатор цикла выводится из события-повода: повторный запуск того же цикла распознаётся по журналу."""
    return "cyc_" + hashlib.sha256(f"{trigger_id}:{round_}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class StageRecord:
    stage: str
    status: str                      # ok | failed | skipped
    events: tuple[str, ...]          # события журнала этой стадии
    epoch: int
    state_version: int               # версия состояния, с которой стадия закончилась
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ActionReport:
    action_id: str
    kind: str
    status: str                      # confirmed | failed | unknown_outcome | not_attempted
    events: tuple[str, ...] = ()
    memory_id: str | None = None


@dataclass
class CycleReport:
    cycle_id: str
    trigger: str
    outcome: str = "pending"         # done | skipped | no_decision | rejected | refused | failed
    reason: str | None = None
    stages: list[StageRecord] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    actions: list[ActionReport] = field(default_factory=list)

    def names(self) -> list[str]:
        return [s.stage for s in self.stages]

    def event_ids(self) -> list[str]:
        return [e for s in self.stages for e in s.events]


class Cycle:
    def __init__(self, controller: StateController, provider: CognitiveProvider, memory: Memory, world: World,
                 capabilities: Capabilities, creator: CreatorBridge, *, attention: AttentionPolicy | None = None,
                 max_rerequest: int = 1, max_actions: int = 1, max_output_tokens: int = 800,
                 deadline_ms: int = 60_000, retrieval_limit: int = 5,
                 allowed_proposals: Iterable[str] = DEFAULT_ALLOWED):
        self.ctl, self.provider, self.memory, self.world = controller, provider, memory, world
        self.capabilities, self.creator = capabilities, creator
        self.attention = attention or AttentionPolicy()
        self.max_rerequest, self.max_output_tokens = max_rerequest, max_output_tokens
        self.deadline_ms, self.retrieval_limit = deadline_ms, retrieval_limit
        self.allowed_proposals = tuple(allowed_proposals)
        for kind, handler in make_kind_handlers(capabilities, max_actions=max_actions).items():
            controller.register_kind(kind, handler)

    # --- один цикл -----------------------------------------------------------------------------

    def run(self, trigger: Event, *, round_: int = 0) -> CycleReport:
        cid = cycle_id(trigger.event_id, round_)
        rep = CycleReport(cid, trigger.event_id)
        if self.ctl.session_state != "running":
            return self._end(rep, "skipped", "session_not_running")
        if self.ctl.find_event(key=f"att:{cid}") is not None:
            return self._end(rep, "skipped", "already_done")
        self._stage(rep, "event", "ok", (trigger.event_id,), {"type": trigger.type})
        focus = self.attention.choose(trigger, self.ctl.state)
        if not self._attend(rep, trigger, focus):
            return rep
        records = self._retrieve(rep, focus)
        self._decide(rep, trigger, focus, records)
        return rep

    def _end(self, rep: CycleReport, outcome: str, reason: str | None = None) -> CycleReport:
        rep.outcome, rep.reason = outcome, reason
        return rep

    def _stage(self, rep: CycleReport, stage: str, status: str, events: Iterable[str] = (), detail: dict | None = None):
        rep.stages.append(StageRecord(stage, status, tuple(events), self.ctl.epoch, self.ctl.state_version,
                                      detail or {}))

    def _move(self, ops: Iterable[Op], reason: str, spec: EventSpec, caused_by: Iterable[str], *, actor: str = "core",
              actor_ref: str = "cycle", transition_id: str | None = None) -> ApplyResult:
        """Один переход состояния на текущей версии; применяет и проверяет привратник."""
        t = Transition(transition_id or new_id("trn"), self.ctl.persona_id, self.ctl.epoch, self.ctl.state_version,
                       tuple(ops), reason, tuple(caused_by))
        return self.ctl.apply(Proposal(t, actor, actor_ref, spec))

    def record_fault(self, fault: str, ident: str, payload: dict) -> None:
        self.ctl.record_event(EventSpec("system.error", f"err:{fault}:{ident}", {"fault": fault, **payload}))

    # --- стадии 2–3: внимание и память ---------------------------------------------------------

    def _attend(self, rep: CycleReport, trigger: Event, focus: Focus) -> bool:
        value = {"focus": focus.focus, "ref": focus.ref, "since_life_ms": self.ctl.clock.now_ms(),
                 "cycle_id": rep.cycle_id}
        spec = EventSpec("attention.chosen", f"att:{rep.cycle_id}", {
            "cycle_id": rep.cycle_id, "trigger": trigger.event_id, "focus": focus.focus, "ref": focus.ref})
        res = self._move([Op("affect", "attention", "set", value, True)], f"внимание: цикл {rep.cycle_id}", spec,
                         (trigger.event_id,))
        ids = tuple(e.event_id for e in res.events[:1])
        self._stage(rep, "attention", "ok" if res.applied else "failed", ids, {"focus": focus.focus, "status": res.status})
        if not res.applied:
            self._end(rep, "failed", f"attention_{res.status}")
        return res.applied

    def _retrieve(self, rep: CycleReport, focus: Focus) -> list[MemoryRecord]:
        # Архив обычному извлечению недоступен, даже если память по ошибке его вернула (E-1).
        records = [r for r in self.memory.retrieve(focus.query, limit=self.retrieval_limit)
                   if r.layer in READABLE_LAYERS]
        res = self.ctl.record_event(EventSpec("memory.retrieved", f"rtv:{rep.cycle_id}:0", {
            "cycle_id": rep.cycle_id, "query": focus.query, "memory_ids": [r.memory_id for r in records]}))
        self._stage(rep, "retrieval", "ok", (e.event_id for e in (res.event,) if e), {"count": len(records)})
        return records

    # --- стадии 4–6: оценка, предложение, проверка ядром ---------------------------------------

    def _context(self, trigger: Event, focus: Focus, records: list[MemoryRecord]) -> list[dict]:
        if trigger.type == "input.arrived":   # только факт «есть новое»: содержимое не прочитано (P-1)
            first = {"ref": focus.ref or trigger.event_id, "origin": "observation", "layer": "notice",
                     "text": "Есть новое сообщение."}
        elif trigger.type == "time.moment_due":
            first = {"ref": trigger.event_id, "origin": "observation", "layer": "state",
                     "text": "Наступил плановый момент." + (f" Дело: {focus.query}" if focus.query else "")}
        else:
            first = {"ref": trigger.event_id, "origin": "observation", "layer": "state", "text": f"Событие {trigger.type}."}
        return [first] + [{"ref": r.memory_id, "origin": r.origin, "layer": r.layer, "text": r.text} for r in records]

    def _request(self, cid: str, context: list[dict], attempt: int, retry_of: str | None) -> dict:
        rid = new_id("req")
        req = {"request_id": rid, "persona_id": self.ctl.persona_id, "session_id": self.ctl.session_id,
               "epoch": self.ctl.epoch, "state_version": self.ctl.state_version,
               "issued_at": self.ctl.clock.calendar_now(), "life_time_ms": self.ctl.clock.now_ms(),
               "purpose": "choose_action", "context": context, "allowed_proposals": list(self.allowed_proposals),
               "allowed_actions": [c.capability_id for c in self.capabilities.list() if c.available],
               "limits": {"max_output_tokens": self.max_output_tokens, "deadline_ms": self.deadline_ms,
                          "attempt": attempt},
               "response_schema": "response.schema.json", "idempotency_key": f"req:{rid}", "cycle_id": cid}
        if retry_of:
            req["retry_of"] = retry_of
        return req

    def _decide(self, rep: CycleReport, trigger: Event, focus: Focus, records: list[MemoryRecord]) -> None:
        attempt, retry_of, stale_left = 0, None, self.max_rerequest
        while True:
            request = self._request(rep.cycle_id, self._context(trigger, focus, records), attempt, retry_of)
            sent = self.ctl.record_request(request)
            if sent.event is None:
                self._end(rep, "no_decision", f"request_{sent.reason}")
                return
            rep.requests.append(request["request_id"])
            self._stage(rep, "appraisal", "ok", (sent.event.event_id,),
                        {"request_id": request["request_id"], "attempt": attempt})
            try:
                response = self.provider.propose(request)
            except Exception as e:   # любой сбой механизма: ошибка в журнал, решения нет
                self.record_fault("provider_error", request["request_id"], {"request_id": request["request_id"],
                                                                      "error": f"{type(e).__name__}: {e}"})
                self._stage(rep, "proposal", "failed", (), {"error": type(e).__name__})
                self._end(rep, "no_decision", "provider_error")
                return
            if tool_use_signs(response):   # R-117: вместе с ответом непригодны все его предложения
                response = {**response, "tool_use_detected": True}
            result = self.ctl.receive_response(response)
            got = (e.event_id for e in (result.received,) if e)
            self._stage(rep, "proposal", "ok" if result.received else "failed", got, {"status": result.status})
            if result.status == "stale" and stale_left > 0:
                stale_left -= 1
                retry_of = request["request_id"] if attempt else None
                continue
            if result.status == "rejected" and result.reason == "bad_format" and attempt == 0:
                attempt, retry_of = 1, request["request_id"]   # одна попытка исправления (E-7)
                continue
            break
        self._verified(rep, response, result)

    def _verified(self, rep: CycleReport, response: dict, result) -> None:
        ok = result.status in ("applied", "partial")
        accepted_events = ({e.payload["proposal_id"]: e.event_id for e in result.transition.events
                            if e.type == "proposal.accepted"} if ok else {})
        self._stage(rep, "verification", "ok" if ok else "failed", accepted_events.values(),
                    {"status": result.status, "reason": result.reason, "rejected": dict(result.rejected)})
        if not ok:
            outcome = {"stale": "no_decision", "ignored": "refused"}.get(result.status, "rejected")
            self._end(rep, outcome, result.reason or result.status)
            return
        accepted = [pr for pr in response["proposals"] if pr["proposal_id"] in accepted_events]
        for pr in accepted:
            if pr["kind"] in EXECUTABLE:
                self._act(rep, pr, accepted_events[pr["proposal_id"]])
            elif pr["kind"] == "creator_request":
                self._ask_creator(rep, pr, accepted_events[pr["proposal_id"]])
        if not any(s.stage == "action" for s in rep.stages):
            self._stage(rep, "action", "skipped", (), {"why": "исполнять нечего: состояние изменено переходом"})
        self._end(rep, "done")

    # --- стадии 7–9: действие, подтверждённый результат, память --------------------------------

    def _act(self, rep: CycleReport, pr: dict, accepted_id: str) -> None:
        body, kind, aid = pr["body"], pr["kind"], action_id_for(pr["proposal_id"])
        request = ActionRequest(aid, kind, body["capability_id"], body.get("params", {}), body.get("to"), body.get("text"))
        attempt_type, attempt_key = _ACTION_EVENTS[kind]["attempt"]
        attempted_id = new_id("evt")
        common = {"action_id": aid, "kind": kind, "capability_id": request.capability_id, "cycle_id": rep.cycle_id,
                  "proposal_id": pr["proposal_id"]}
        path = f"world.actions.{aid}"
        res = self._move([Op("world_time", f"{path}.status", "transition", "attempted", True),
                          Op("world_time", f"{path}.attempted_event", "set", attempted_id, True)],
                         f"попытка действия {aid}", EventSpec(attempt_type, attempt_key.format(aid), common,
                                                              event_id=attempted_id), (accepted_id,))
        self._stage(rep, "action", "ok" if res.applied else "failed", (attempted_id,) if res.applied else (),
                    {"action_id": aid, "status": res.status})
        if not res.applied:
            rep.actions.append(ActionReport(aid, kind, "not_attempted"))
            return
        try:
            outcome = self.world.execute(request)
            if not isinstance(outcome, ActionOutcome):
                raise TypeError(f"мир вернул {type(outcome).__name__} вместо ActionOutcome")
        except Exception as e:   # обрыв или мусор вместо ответа: исход неизвестен, не успех
            self.record_fault("action_crash", aid, {"action_id": aid, "error": f"{type(e).__name__}: {e}"})
            outcome = ActionOutcome("unknown", "обрыв при выполнении")
        done = self._settle(aid, kind, outcome, attempted_id, common)
        confirmed = outcome.status == "confirmed" and done.applied
        self._stage(rep, "confirmation", "ok" if done.applied else "failed", (e.event_id for e in done.events[:1]),
                    {"action_id": aid, "outcome": outcome.status, "status": done.status})
        memory_id = self._remember(rep, request, outcome, done.events[0]) if confirmed else None
        rep.actions.append(ActionReport(aid, kind, _RESULT_STATUS[outcome.status] if done.applied else "attempted",
                                        tuple(e.event_id for e in done.events[:1]), memory_id))

    def _settle(self, aid: str, kind: str, outcome: ActionOutcome, attempted_id: str, common: dict) -> ApplyResult:
        """Записать исход. Подтверждение и неудачу пишет подтверждающий источник (мир), «неизвестно» — ядро."""
        etype, ekey = _ACTION_EVENTS[kind][outcome.status]
        actor, ref = ("core", "cycle") if outcome.status == "unknown" else ("world", type(self.world).__name__)
        path = f"world.actions.{aid}"
        return self._move([Op("world_time", f"{path}.status", "transition", _RESULT_STATUS[outcome.status], True),
                           Op("world_time", f"{path}.outcome", "set", outcome.summary, True)],
                          f"исход действия {aid}: {outcome.status}",
                          EventSpec(etype, ekey.format(aid), {**common, "summary": outcome.summary}),
                          (attempted_id,), actor=actor, actor_ref=ref)

    def _remember(self, rep: CycleReport, request: ActionRequest, outcome: ActionOutcome, confirmed: Event) -> str | None:
        """Воспоминание о действии — только после подтверждения и только отдельным переходом (R-199)."""
        text = self.memory.describe(request, outcome)
        if text is None:
            self._stage(rep, "memory_update", "skipped", (), {"why": "запоминать нечего"})
            return None
        rec = MemoryRecord(memory_id=new_id("mem"), persona_id=self.ctl.persona_id, layer="personal", kind="episodic",
                           origin="observation", content={"text": text}, source=Source("core", "cycle"),
                           created_at=confirmed.occurred_at, created_life_ms=confirmed.life_time_ms,
                           sources=(confirmed.event_id,))
        tid = new_id("trn")
        res = self._move([Op("memory", rec.memory_id, "add", rec.to_dict(), True)], f"воспоминание о {request.action_id}",
                         EventSpec("memory.recorded", f"mem:{tid}:0", {"memory_id": rec.memory_id, "cycle_id": rep.cycle_id,
                                                                    "action_id": request.action_id}),
                         (confirmed.event_id,), transition_id=tid)
        self._stage(rep, "memory_update", "ok" if res.applied else "failed", (e.event_id for e in res.events[:1]),
                    {"memory_id": rec.memory_id, "status": res.status})
        return rec.memory_id if res.applied else None

    def _ask_creator(self, rep: CycleReport, pr: dict, accepted_id: str) -> None:
        key, text = pr["body"]["request_key"], pr["body"]["text"]
        crq = new_id("crq")
        path = f"world.creator_requests.{key}"
        res = self._move([Op("world_time", f"{path}.status", "set", "open", True),
                          Op("world_time", f"{path}.crq_id", "set", crq, True)], f"просьба оператору {key}",
                         EventSpec("creator.request", f"crq:{key}", {"crq_id": crq, "request_key": key, "text": text,
                                                                    "cycle_id": rep.cycle_id}), (accepted_id,))
        self._stage(rep, "action", "ok" if res.applied else "failed", (e.event_id for e in res.events[:1]),
                    {"creator_request": key, "status": res.status})
        if not res.applied:
            return
        try:
            delivery = self.creator.submit(CreatorRequest(crq, key, text))
        except Exception as e:
            delivery = Delivery(False, f"{type(e).__name__}: {e}")
        if not delivery.delivered:   # просьба остаётся открытой, но «доставлена» не объявляется
            self.record_fault("creator_undelivered", crq, {"crq_id": crq, "detail": delivery.detail})

    # --- после сбоя: что осталось «в попытке» --------------------------------------------------

    def settle_unfinished(self) -> list[str]:
        """Действия со статусом «попытка» без исхода (обрыв процесса) получают «исход неизвестен» —
        не успех и не провал. Вызывать после возобновления сеанса."""
        if self.ctl.session_state != "running":
            return []
        actions = self.ctl.state["world_time"].get("world", {}).get("actions", {})
        settled = []
        for aid, rec in sorted(actions.items()):
            if rec["status"] != "attempted":
                continue
            common = {"action_id": aid, "kind": rec["kind"], "capability_id": rec["capability_id"],
                      "cycle_id": rec["cycle_id"], "proposal_id": rec["proposal_id"]}
            res = self._settle(aid, rec["kind"], ActionOutcome("unknown", "процесс оборвался, исход неизвестен"),
                               rec["attempted_event"], common)
            if res.applied:
                settled.append(aid)
        return settled
