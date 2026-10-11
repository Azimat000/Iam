"""Общие заготовки для тестов хранилища."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from iam.domain import Event, MemoryRecord, Source, new_id
from iam.storage import Store

PID = "per_TestPersona01"
EXP = "exp_TestPersona01"
T0 = "2026-10-11T18:00:00Z"
HERE = Path(__file__).resolve().parent


def ev(n: int, *, persona=PID, type_="input.arrived", key=None, payload=None, life=None, version=0, causes=()):
    """Событие: по умолчанию — поступление сообщения n (не меняет состояние)."""
    changes = type_ != "input.arrived"
    if key is None:
        key = f"rx:tg:{n}" if type_ == "input.arrived" else f"mem:trn_{n:08d}:0"
    return Event(event_id=new_id("evt"), persona_id=persona, type=type_, occurred_at=T0,
                 life_time_ms=n * 10 if life is None else life, source=Source("receiver", "telegram"),
                 idempotency_key=key, epoch=1, state_version=version, changes_state=changes,
                 payload={"n": n} if payload is None else payload, causes=tuple(causes))


def mem(text: str, *, layer="personal", persona=PID, origin="observation", mid=None, version=1, **extra):
    return MemoryRecord(memory_id=mid or new_id("mem"), persona_id=persona, layer=layer, kind="episodic",
                        origin=origin, content={"text": text}, source=Source("core", "test"), created_at=T0,
                        created_life_ms=0, sources=("evt_ABCDEFGH",), version=version, **extra)


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="iam-s04-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "p.db"

    def new_store(self, persona=PID, path=None) -> Store:
        s = Store.create(path or self.db, persona)
        self.addCleanup(s.close)
        return s


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HERE.parents[1] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env
