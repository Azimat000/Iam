"""Общие заготовки тестов цикла: работающая личность, заглушки всех шести интерфейсов, событие-повод."""
from iam.clock import life_moment
from iam.cycle import Cycle, Runner
from iam.domain import Op, new_id
from iam.interfaces import Capability
from iam.stubs import (ScriptedProvider, StubCapabilities, StubCreatorBridge, StubMemory, StubWorld)

from state.helpers import StateCase, set_

CAP_NOTES = "cap_Notes00001"
CAP_TG = "cap_Telegram01"
CAP_PLOT = "cap_Plotting01"      # в реестре не зарегистрирована
INTENTION = "int_ReadBook01"

__all__ = ["CycleCase", "CAP_NOTES", "CAP_TG", "CAP_PLOT", "INTENTION", "set_"]


class CycleCase(StateCase):
    """Личность с одним желанием в состоянии и заглушками вместо модели, мира, памяти, реестра и оператора."""

    def build(self, *replies, world=None, memory=None, creator=None, caps=None, **cycle_kw):
        self.c = self.controller(running=True)
        self.assertTrue(self.c.apply(self.prop(self.c, Op("intentions", INTENTION, "set", {
            "text": "дочитать главу 4", "status": "active"}, True))).applied)
        self.provider = ScriptedProvider(replies)
        self.world = world or StubWorld()
        self.caps = caps or StubCapabilities([Capability(CAP_NOTES, "заметки", "писать заметки"),
                                              Capability(CAP_TG, "сообщение оператору", "написать оператору")])
        self.memory = memory or StubMemory()
        self.creator = creator or StubCreatorBridge()
        self.cycle = Cycle(self.c, self.provider, self.memory, self.world, self.caps, self.creator, **cycle_kw)
        self.runner = Runner(self.cycle, self.c, self.creator)
        return self.cycle

    def due(self, owner_ref=INTENTION):
        """Наступивший плановый момент → событие `time.moment_due` (повод для цикла)."""
        mid = new_id("mom")
        self.assertTrue(self.c.schedule_moment(life_moment(mid, self.c.life_time_ms, owner_ref)).applied)
        (res,) = self.c.fire_due_moments()
        self.assertTrue(res.applied)
        return res.events[0]

    def journal(self, report):
        """События цикла из журнала, по порядку записи."""
        return sorted((self.c.find_event(event_id=i) for i in report.event_ids()), key=lambda e: e.seq)

    def types(self, c=None):
        return [e.type for e in (c or self.c).store.events()]
