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
    def __init__(self, cycle: Cycle, controller: StateController, creator: CreatorBridge, *, max_cycles: int = 5,
                 triggers: tuple[str, ...] = TRIGGERS):
        self.cycle, self.ctl, self.creator = cycle, controller, creator
        self.max_cycles, self.triggers = max_cycles, triggers
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
        циклы на накопившиеся поводы (не больше `max_cycles`, остальные ждут следующего вызова)."""
        self.ingest()
        if self.ctl.session_state != "running":
            return []
        self.ctl.tick()
        self.cycle.settle_unfinished()
        fresh = self.ctl.events_after(self.cursor)
        if fresh:
            self.cursor = fresh[-1].seq
        todo = [*self.backlog, *((e, 0) for e in fresh if e.type in self.triggers)]
        self.backlog = todo[self.max_cycles:]
        reports = []
        for event, round_ in todo[:self.max_cycles]:
            report = self.cycle.run(event, round_=round_)
            if report.outcome == "no_decision":
                self.backlog.append((event, round_ + 1))
            reports.append(report)
        return reports
