"""Подпроцесс для тестов аварии и замка (не тест; запускается тестами).

  child.py hold <db>              — открыть личность для записи, напечатать READY и ждать (замок)
  child.py crash <db> <stop_tx>   — писать переходы бесконечно; в переходе №stop_tx остановиться
                                    посреди транзакции, напечатать «MID <размер WAL>» и ждать kill -9
                                    (0 — не останавливаться)

Кэш страниц урезан, данные крупные, а перед переходом-остановкой WAL обнуляется: незафиксированные
страницы физически попадают в файл WAL до убийства, и тест проверяет именно восстановление после
частично записанного журнала, а не только потерю памяти процесса.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from iam.domain import Event, MemoryRecord, Source, new_id  # noqa: E402
from iam.storage import Snapshot, Store  # noqa: E402

EVENTS_PER_TX = 6
BULK = "данные " * 700  # ~9 КБ на событие: страницы не помещаются в урезанный кэш
T0 = "2026-10-11T18:00:00Z"


def wal_size(store: Store) -> int:
    wal = Path(f"{store.path}-wal")
    return wal.stat().st_size if wal.exists() else 0


def transition(store: Store, stop_here: bool) -> None:
    if stop_here:
        store.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with store.transaction():
        snap = store.latest_snapshot()
        version = (snap.state_version if snap else 0) + 1
        trn = new_id("trn")
        last = None
        for i in range(EVENTS_PER_TX):
            if stop_here and i == EVENTS_PER_TX // 2:
                print(f"MID {wal_size(store)}", flush=True)
                time.sleep(60)
            last, _ = store.append_event(Event(
                event_id=new_id("evt"), persona_id=store.persona_id, type="memory.recorded", occurred_at=T0,
                life_time_ms=version, source=Source("core", "crash-test"), idempotency_key=f"mem:{trn}:{i}",
                epoch=1, state_version=version, changes_state=True, payload={"transition_id": trn, "i": i, "bulk": BULK}))
        store.add_memory(MemoryRecord(
            memory_id="mem_" + trn[4:], persona_id=store.persona_id, layer="personal", kind="episodic",
            origin="observation", content={"text": f"переход {trn}"}, source=Source("core", "crash-test"),
            created_at=T0, created_life_ms=version, sources=(last.event_id,)))
        store.put_snapshot(Snapshot(1, version, last.seq, version, T0, {"transition_id": trn}))


def main(argv):
    mode, db = argv[0], argv[1]
    store = Store.open(db)
    if mode == "hold":
        print("READY", flush=True)
        time.sleep(60)
        return
    stop_tx = int(argv[2])
    store.con.execute("PRAGMA cache_size = 2")
    print("STARTED", flush=True)
    n = 0
    while True:
        n += 1
        transition(store, n == stop_tx)


if __name__ == "__main__":
    main(sys.argv[1:])
