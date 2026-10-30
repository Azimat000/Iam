"""Общие часы, память, мир и граница с проверяющим ядром для вариантов A/B/C.

Новые варианты реализуют build(runtime) -> Cycle и могут использовать runtime.cycle(provider).
Стенд не выбирает за них ответ и не объявляет смысловые утверждения сценария пройденными.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import random

from iam.clock import LifeClock, life_moment
from iam.cycle import Cycle
from iam.cycle.runner import input_id_for
from iam.domain import MemoryRecord, Op, Source, Transition, new_id
from iam.interfaces import Capability
from iam.state import EventSpec, Proposal, StateController
from iam.stubs import StubCapabilities, StubCreatorBridge, StubWorld

from .memory import BenchMemory

CAP_NOTES = "cap_ExperimentNotes01"
CALENDAR_START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


class SimTime:
    def __init__(self):
        self.life_ms = 0
        self.calendar_ms = 0

    def monotonic(self):
        return self.life_ms * 1_000_000

    def calendar(self):
        return (CALENDAR_START + dt.timedelta(milliseconds=self.calendar_ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ExperimentCycle(Cycle):
    def __init__(self, runtime, provider, **kwargs):
        self.runtime = runtime
        super().__init__(runtime.controller, provider, runtime.memory, runtime.world,
                         runtime.capabilities, runtime.creator, **kwargs)

    def _context(self, trigger, focus, records):
        base = super()._context(trigger, focus, records)
        # situation — описание выбора из билета; скрытые входящие и выключенный опыт сюда не подмешиваются.
        return base + [{"ref": trigger.event_id, "origin": "simulation_event", "layer": "state",
                        "text": self.runtime.situation},
                       {"ref": trigger.event_id, "origin": "simulation_event", "layer": "state",
                        "text": "Профиль: " + self.runtime.persona.name + "; мотивы: " +
                                "; ".join(self.runtime.motives)}]


class Runtime:
    def __init__(self, store, persona, seed, switches):
        self.persona = copy.deepcopy(persona)
        self.rng = random.Random(seed)
        self.switches = dict(switches)
        self.time = SimTime()
        self.controller = StateController(store, clock=LifeClock(monotonic=self.time.monotonic,
                                                                 calendar=self.time.calendar))
        self.memory = BenchMemory(self.controller, off=switches["memory_off"])
        self.world = StubWorld()
        self.capabilities = StubCapabilities([Capability(CAP_NOTES, "заметки в экспериментальном мире")])
        self.creator = StubCreatorBridge()
        self.motives = [] if switches["motivation_off"] else list(persona.motives)
        self.situation = ""
        self.model_steps = []
        self._trigger = None
        self.require(self.controller.create_persona({"traits": copy.deepcopy(persona.traits)}))

    @staticmethod
    def require(result):
        if result.status not in ("applied", "recorded", "duplicate"):
            raise ValueError(f"ядро отклонило шаг стенда: {result}")
        return result

    def cycle(self, provider, **kwargs):
        return ExperimentCycle(self, provider, **kwargs)

    def move(self, ops, reason, *, carrier=None, cause=None, actor="core"):
        c = self.controller
        cause = cause or c.events_after(0)[-1].event_id
        tr = Transition(new_id("trn"), c.persona_id, c.epoch, c.state_version, tuple(ops), reason, (cause,))
        return self.require(c.apply(Proposal(tr, actor, "experiment_fixture", carrier)))

    def background(self, items):
        """Свободный текст S03 сохраняется без догадок о его значении; толкование — задача варианта.

        Память получает simulation_event; исходный origin сохраняется в содержимом записи.
        Иные виды предыстории доступны варианту отдельно от выключаемых мотивов и памяти.
        """
        c = self.controller
        for n, item in enumerate(items):
            logical = item.get("id", f"background_{n}")
            if item["kind"] == "memory":
                mid = "mem_" + hashlib.sha256(logical.encode()).hexdigest()[:16]
                cause = c.events_after(0)[-1].event_id
                rec = MemoryRecord(mid, c.persona_id, "personal", "semantic", "simulation_event",
                                   {"text": item["text"], "scenario_origin": item.get("origin", "simulation_event"),
                                    "logical_id": logical}, Source("core", "experiment_fixture"),
                                   c.clock.calendar_now(), c.life_time_ms, (cause,))
                tid = new_id("trn")
                self.move([Op("memory", mid, "add", rec.to_dict(), True)], "экспериментальная предыстория",
                          carrier=EventSpec("memory.recorded", f"mem:{tid}:0", {"memory_id": mid}), cause=cause)
            elif item["kind"] == "intention":
                # При старте намерения всегда active; переход к waiting/deferred — отдельное решение варианта.
                self.move([Op("intentions", logical, "set", {"text": item["text"], "status": "active"}, True)],
                          "намерение из предыстории")
            else:
                self.move([Op("world_time", f"world.background.{logical}", "set", item, True)], "предыстория среды")

    def read_input(self, input_id):
        """Явное чтение варианта: до вызова тело сообщения не попадает в контекст цикла."""
        arrived = next(e for e in self.controller.events_after(0)
                       if e.type == "input.arrived" and e.payload["input_id"] == input_id)
        result = self.move([Op("affect", f"read_inputs.{input_id}", "set", arrived.payload["text"], True)],
                           "прочитано сообщение", carrier=EventSpec("input.read", f"rd:{input_id}",
                           {"input_id": input_id}), cause=arrived.event_id)
        return arrived.payload["text"], result.events[0].event_id

    def step(self, step):
        c, kind = self.controller, step["kind"]
        if kind == "session":
            fn = {"start": c.start_session, "pause": c.pause_session,
                  "resume": c.resume_session, "stop": c.stop_session}[step["action"]]
            self.require(fn())
        elif kind == "wait":
            ms = step.get("life_ms", step.get("calendar_ms"))
            if "life_ms" in step:
                if c.session_state != "running":
                    raise ValueError("время жизни не идёт в паузе")
                self.time.life_ms += ms
            self.time.calendar_ms += ms
            c.tick()
        elif kind == "input":
            external = step["id"]
            iid = input_id_for("experiment", external)
            for _ in range(step.get("deliveries", 1)):
                res = self.require(c.record_event(EventSpec("input.arrived", f"rx:experiment:{external}",
                                   {"input_id": iid, "sender": step["sender"], "channel": "experiment",
                                    "text": step["text"]}), actor_ref="experiment_receiver"))
            self._trigger = res.event
            if c.session_state == "running":
                self.move([Op("affect", f"notices.{iid}", "set", {"notice_level": "N2", "sender": step["sender"]}, True)],
                          "уведомление без содержимого", carrier=EventSpec("input.noticed", f"ntc:{iid}:N2",
                          {"input_id": iid, "notice_level": "N2", "sender": step["sender"]}), cause=res.event.event_id)
        elif kind == "model":
            self.model_steps.append(copy.deepcopy(step))
        elif kind == "world":
            self.world_result(step)

    def world_result(self, step):
        """Заранее заданное занятие мира. Это свидетельство симуляции, не вызов внешнего инструмента."""
        aid = "act_" + hashlib.sha256(step["id"].encode()).hexdigest()[:16]
        path = f"world.fixture_activities.{aid}"
        started = self.move([Op("world_time", path, "set", {"text": step["action"], "status": "started"}, True)],
                            "начало занятия в симуляции", carrier=EventSpec("activity.started", f"act:{aid}:start",
                            {"action_id": aid, "simulation": True}))
        attempted = self.move([Op("world_time", path + ".status", "set", "attempted", True)], "попытка в симуляции",
                              carrier=EventSpec("action.attempted", f"act:{aid}:try", {"action_id": aid, "simulation": True}),
                              cause=started.events[0].event_id)
        outcome = {"confirmed": "confirmed", "failed": "failed", "no_answer": "unknown_outcome"}[step["outcome"]]
        suffix = {"confirmed": "ok", "failed": "fail", "unknown_outcome": "unk"}[outcome]
        event_type = "action." + outcome
        result = self.move([Op("world_time", path + ".status", "set", outcome, True)], "исход симуляции",
                           carrier=EventSpec(event_type, f"act:{aid}:{suffix}", {"action_id": aid, "simulation": True}),
                           actor="core" if outcome == "unknown_outcome" else "world", cause=attempted.events[0].event_id)
        if outcome == "confirmed":
            result = self.move([Op("world_time", path + ".status", "set", "done", True)], "занятие завершено",
                               carrier=EventSpec("activity.completed", f"act:{aid}:done",
                               {"action_id": aid, "simulation": True, "summary": step["action"]}),
                               actor="world", cause=result.events[0].event_id)
        self._trigger = result.events[0]

    def trigger(self, step_id):
        if self._trigger is not None:
            trigger, self._trigger = self._trigger, None
            return trigger
        mid = "mom_" + hashlib.sha256(step_id.encode()).hexdigest()[:16]
        c = self.controller
        self.require(c.schedule_moment(life_moment(mid, c.life_time_ms, "experiment")))
        results = c.fire_due_moments()
        return results[-1].events[0]
