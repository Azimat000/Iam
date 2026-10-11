"""Подпроцесс для тестов привратника (не тест; запускается тестами).

  child_state.py run <db>   — создать личность (если её нет), начать/продолжить сеанс и бесконечно применять
                              переходы с сопутствующими событиями; после первого напечатать READY
  child_state.py try <db>   — попытаться открыть личность для записи: печатает OPENED или LOCKED
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from iam.domain import Op, Transition, new_id  # noqa: E402
from iam.state import EventSpec, Proposal, StateController  # noqa: E402
from iam.storage import AlreadyLocked, Store  # noqa: E402

PID = "per_ChildPersona01"
BULK = "значение " * 150


def main(mode: str, db: str) -> None:
    path = Path(db)
    if mode == "try":
        try:
            Store.open(path).close()
        except AlreadyLocked:
            print("LOCKED", flush=True)
        else:
            print("OPENED", flush=True)
        return
    store = Store.open(path) if path.exists() else Store.create(path, PID)
    c = StateController(store)
    if c.state_version == 0:
        c.create_persona({"traits": {"curious": {"value": 0.5}}})
    if c.session_state == "stopped":
        c.start_session()
    elif c.session_state == "paused":
        c.resume_session()
    root = store.get_event(1).event_id
    i = 0
    while True:
        i += 1
        t = Transition(new_id("trn"), PID, c.epoch, c.state_version,
                       (Op("beliefs", f"b{i % 7}", "set", {"n": i, "text": BULK}, True),
                        Op("affect", "reactions", "add", i, True)), "шаг подпроцесса", (root,))
        mark = EventSpec("proposal.accepted", f"prop:prop_{new_id('prop')[5:]}:accept")
        res = c.apply(Proposal(t, "core", companions=(mark,)))
        assert res.applied, res
        if i == 1:
            print("READY", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:3])
