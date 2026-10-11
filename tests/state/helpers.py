"""Общие заготовки тестов привратника: подменяемые часы и фабрика контроллера."""
import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from iam.clock import LifeClock
from iam.domain import Event, MemoryRecord, Op, Source, Transition, new_id
from iam.state import EventSpec, Proposal, StateController
from iam.storage import Store

PID = "per_TestPersona01"
EXP = "exp_TestPersona01"
START = dt.datetime(2026, 10, 11, 10, 0, 0, tzinfo=dt.UTC)


class FakeTime:
    """Монотонные и календарные часы под полным контролем теста. `advance` двигает оба, `jump_calendar` —
    только календарные (перевод часов, смена пояса), `advance_mono` — только монотонные."""

    def __init__(self):
        self.mono_ns = 5_000_000_000
        self.cal = START

    def mono(self) -> int:
        return self.mono_ns

    def calendar(self) -> str:
        return self.cal.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def advance(self, ms: int) -> None:
        self.mono_ns += ms * 1_000_000
        self.cal += dt.timedelta(milliseconds=ms)

    def advance_mono(self, ms: int) -> None:
        self.mono_ns += ms * 1_000_000

    def jump_calendar(self, ms: int) -> None:
        self.cal += dt.timedelta(milliseconds=ms)


class StateCase(unittest.TestCase):
    """Каждый тест — своя временная база и свои часы."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="iam-s05-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "p.db"
        self.time = FakeTime()

    def clock(self, initial=0, **kw) -> LifeClock:
        return LifeClock(initial, monotonic=self.time.mono, calendar=self.time.calendar, **kw)

    def controller(self, *, persona=PID, created=True, running=False, **kw) -> StateController:
        """Контроллер новой личности. Пока сеанс не запущен, обычные переходы запрещены (time.md#sessions),
        поэтому тестам, меняющим состояние, нужен `running=True`."""
        store = Store.create(self.db, persona)
        self.addCleanup(store.close)
        c = StateController(store, clock=kw.pop("clock", None) or self.clock(), **kw)
        if created:
            self.assertTrue(c.create_persona({"traits": {"curious": {"value": 0.5}}}).applied)
        if running:
            self.assertTrue(c.start_session().applied)
        return c

    def reopen(self, c: StateController, **kw) -> StateController:
        """Перезапуск процесса: закрыть хранилище (отпустить замок) и открыть заново с новыми часами."""
        c.store.close()
        store = Store.open(self.db)
        self.addCleanup(store.close)
        return StateController(store, clock=kw.pop("clock", None) or LifeClock.recover(
            store, monotonic=self.time.mono, calendar=self.time.calendar), **kw)

    @staticmethod
    def root(c: StateController) -> str:
        """Событие-основание, которое есть в журнале (первое событие личности)."""
        return c.store.get_event(1).event_id

    def tr(self, c: StateController, *ops: Op, cause=None, base=None, proposal_id=None, reason="тестовое изменение"):
        epoch, version = base or (c.epoch, c.state_version)
        return Transition(new_id("trn"), c.persona_id, epoch, version, tuple(ops), reason,
                          (cause or self.root(c),), proposal_id)

    def prop(self, c, *ops, actor="core", **kw) -> Proposal:
        return Proposal(self.tr(c, *ops, **{k: v for k, v in kw.items() if k in ("cause", "base", "proposal_id", "reason")}),
                        actor, **{k: v for k, v in kw.items() if k in ("carrier", "companions", "request_id")})

    def types(self, c: StateController) -> list[str]:
        return [e.type for e in c.store.events()]


def set_(group, path, value) -> Op:
    return Op(group, path, "set", value, True)


def plain_event(life: int, n: int = 1, persona: str = PID) -> Event:
    """Событие приёма сообщения (не меняет состояние) для тестов, которые пишут прямо в хранилище."""
    return Event(event_id=new_id("evt"), persona_id=persona, type="input.arrived", occurred_at="2026-10-11T10:00:00.000Z",
                 life_time_ms=life, source=Source("receiver", "telegram"), idempotency_key=f"rx:tg:{n}", epoch=1,
                 state_version=0, changes_state=False, payload={"n": n})


def memory(text="чай с мятой", *, mid="mem_AAAAAAAA", persona=PID, layer="personal", origin="stated_by_other", version=1,
           **extra) -> dict:
    """Запись памяти в виде словаря (значение операции `memory`)."""
    kw = dict(claim_kind="statement_made", independence_group="src_operator") if origin == "stated_by_other" else {}
    kw.update(extra)
    rec = MemoryRecord(memory_id=mid, persona_id=persona, layer=layer, kind="semantic", origin=origin,
                       content={"text": text}, source=Source("core", "test"), created_at="2026-10-11T10:00:00.000Z",
                       created_life_ms=0, sources=("evt_ABCDEFGH",), version=version, **kw)
    return rec.to_dict()
