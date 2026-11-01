"""Запуск циклов по событиям. Фонового опроса модели нет (R-036): раннер смотрит журнал, находит события-поводы
(`time.moment_due`, `input.arrived`, `activity.completed`) и на каждое запускает один цикл. Нет поводов — нет циклов
и нет обращений к провайдеру. Предел `max_cycles` ограничивает число решений за один вызов."""
from __future__ import annotations

import hashlib

from iam.domain import Event
from iam.interfaces import CreatorBridge, StateController
from iam.state import EventSpec

from .cycle import TRIGGERS, Cycle, CycleReport


def input_id_for(channel: str, external_id: str) -> str:
    """Идентификатор входящего выводится из канала и внешнего номера: повторная доставка даёт тот же `inp_…`."""
    return "inp_" + hashlib.sha256(f"{channel}:{external_id}".encode()).hexdigest()[:16]


class Runner:
    def __init__(self, cycle: Cycle, controller: StateController, creator: CreatorBridge, *, max_cycles: int = 5, max_rounds: int = 3,
                 triggers: tuple[str, ...] | None = None):
        if triggers is None:
            triggers = (*TRIGGERS, 'input.read') if cycle.perception is not None else TRIGGERS
        self.cycle, self.ctl, self.creator = cycle, controller, creator
        self.max_cycles, self.max_rounds, self.triggers = max_cycles, max_rounds, triggers
        self.cursor = 0                                 # последний просмотренный seq журнала
        self.backlog: list[tuple[Event, int]] = []      # поводы, оставшиеся «на следующий цикл»

    def ingest(self) -> int:
        """Принять сообщения оператора как события `input.arrived` (в паузе тоже). Содержимое лежит в журнале,
        но в запросы к модели не попадает, пока оно не прочитано (S20)."""
        n = 0
        for m in self.creator.poll_replies():
            res = self.ctl.record_event(EventSpec("input.arrived", f"rx:{m.channel}:{m.external_id}", {
                "input_id": input_id_for(m.channel, m.external_id), "channel": m.channel, "sender": m.sender,
                "text": m.text}), actor_ref="creator_bridge")
            n += res.status == "recorded"
        return n

    def run_pending(self) -> list[CycleReport]:
        """Один проход: принять входящее; если сеанс идёт — обслужить часы и «зависшие» действия и запустить
        циклы на накопившиеся поводы (не больше `max_cycles` решений, остальные ждут следующего вызова).
        Пропущенные («уже было») в предел не входят. Исключение внутри цикла не роняет проход: оно пишется
        в журнал, а повод не повторяется (иначе модель могла бы предложить действие второй раз)."""
        self.ingest()
        if self.ctl.session_state != "running":
            return []
        self.ctl.tick()
        self.cycle.settle_unfinished()
        fresh = self.ctl.events_after(self.cursor)
        if fresh:
            self.cursor = fresh[-1].seq
        todo = [*self.backlog, *((e, 0) for e in fresh if e.type in self.triggers)]
        self.backlog, reports = [], []
        for i, (event, round_) in enumerate(todo):
            if len(reports) >= self.max_cycles:
                self.backlog = todo[i:]
                break
            try:
                report = self.cycle.run(event, round_=round_)
            except Exception as e:
                self.cycle.record_fault("cycle_crash", event.event_id,
                                        {"trigger": event.event_id, "error": f"{type(e).__name__}: {e}"})
                continue
            if report.outcome == "skipped":
                continue
            if report.outcome == "no_decision":
                if round_ + 1 < self.max_rounds:
                    self.backlog.append((event, round_ + 1))
                else:
                    self.cycle.record_fault("cycle_gave_up", event.event_id,
                                            {"trigger": event.event_id, "rounds": self.max_rounds})
            reports.append(report)
        return reports
