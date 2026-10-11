"""Общие заготовки: личность с историей, быстрые копии с разными моментами времени."""
import datetime as dt
from pathlib import Path

from iam.domain import Source
from iam.state import EventSpec
from iam.storage import InboxItem

from state.helpers import PID, StateCase, memory, set_

T0 = dt.datetime(2026, 10, 11, 12, 0, 0, tzinfo=dt.UTC)

__all__ = ["BackupCase", "PID", "T0", "memory", "set_"]


class BackupCase(StateCase):
    """Личность с каталогами `live/` (база) и `backups/`; `tick()` двигает время, чтобы копии различались."""

    def setUp(self):
        super().setUp()
        self.live = self.tmp / "live"
        self.live.mkdir()
        self.db = self.live / f"{PID}.db"
        self.backups = self.tmp / "backups"
        self._n = 0

    def tick(self) -> dt.datetime:
        self._n += 1
        return T0 + dt.timedelta(seconds=self._n)

    def persona(self, **kw):
        """Работающая личность с одним фактом в памяти и одной записью о входящем сообщении."""
        c = self.controller(running=True, **kw)
        c.apply(self.prop(c, set_("traits", "curious", {"value": 0.6})))
        c.apply(self.prop(c, __import__("iam.domain", fromlist=["Op"]).Op(
            "memory", "mem_AAAAAAAA", "add", memory("До копии: она прочитала главу 1."), True)))
        return c

    def live_more(self, c, text="Теперь прочитай главу 2 и расскажи, что понравилось."):
        """Жизнь после копии: сообщение и переход состояния."""
        n = c.store.last_seq() + 1
        c.record_event(EventSpec("input.arrived", f"rx:tg:{n}", {"text": text}))
        c.apply(self.prop(c, set_("traits", "curious", {"value": 0.9})))
        return c
