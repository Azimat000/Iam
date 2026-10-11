"""Случайные прогоны цикла: что бы ни отвечали модель и мир, выполненным объявляется только подтверждённое мигом
«мира», воспоминание о действии есть только у подтверждённого, а воспроизведение журнала сходится с состоянием."""
import random
import unittest

from iam.domain import Op
from iam.interfaces import ProviderError
from iam.state import verify
from iam.stubs import Reply, StubWorld, act, ask, attend, change, rest, say

from .helpers import CAP_NOTES, CAP_PLOT, CAP_TG, CycleCase

SEEDS = 40


def random_reply(rng: random.Random, c):
    pick = rng.randrange(12)
    if pick == 0:
        return Reply((act(CAP_NOTES, {"n": rng.randrange(9)}),))
    if pick == 1:
        return Reply((say(CAP_TG, "operator", f"привет {rng.randrange(9)}"),))
    if pick == 2:
        return Reply((act(CAP_PLOT),))                                       # возможности нет
    if pick == 3:
        return Reply((rest("тихо"), attend("rest")))                         # конфликт полей
    if pick == 4:
        return Reply((ask(f"k{rng.randrange(3)}", "помоги"),))
    if pick == 5:
        bad = act(CAP_NOTES)
        bad["body"][rng.choice(["executed", "tool_calls", "stdout", "result"])] = True
        return Reply((bad,))
    if pick == 6:
        return Reply(raw={"мусор": rng.randrange(9)})
    if pick == 7:
        return Reply(error=ProviderError("лимит"))
    if pick == 8:
        return Reply((act(CAP_NOTES), say(CAP_TG, "operator", "два дела")))   # больше предела решений
    if pick == 9:
        return Reply((act(CAP_NOTES),), before=lambda req: c.apply(
            _prop(c, Op("preferences", f"s{rng.randrange(99)}", "set", 1, True))))   # ответ устареет
    if pick == 10:
        return Reply((act(CAP_NOTES),), tool_use=True)
    return Reply((change([{"group": "world_time", "path": "session_state", "op": "set", "value": "stopped"}]),))


def _prop(c, op):
    from iam.domain import Transition, new_id
    from iam.state import Proposal
    return Proposal(Transition(new_id("trn"), c.persona_id, c.epoch, c.state_version, (op,), "помеха",
                               (c.store.get_event(1).event_id,)))


class CycleFuzzTest(CycleCase):
    def test_random_runs_keep_the_honesty_invariants(self):
        seen = {}
        for seed in range(SEEDS):
            with self.subTest(seed=seed):
                self.run_seed(seed)
                for e in self.c.store.events():
                    seen[e.type] = seen.get(e.type, 0) + 1
                self.tearDownSeed()
        # прогоны не вакуумны: были и подтверждения, и неудачи, и «неизвестно», и отказы, и устаревшие ответы
        for needed in ("action.confirmed", "action.failed", "action.unknown_outcome", "message.out_sent",
                       "message.out_unknown", "proposal.rejected", "proposal.stale", "memory.recorded", "creator.request",
                       "system.error"):
            self.assertGreater(seen.get(needed, 0), 0, f"за {SEEDS} прогонов не встретилось {needed}")

    def tearDownSeed(self):
        self.c.store.close()
        self.db.unlink()
        for suffix in ("-wal", "-shm", ".lock"):
            self.db.with_name(self.db.name + suffix).unlink(missing_ok=True)

    def run_seed(self, seed):
        rng = random.Random(seed)
        world = StubWorld()
        for cap in (CAP_NOTES, CAP_TG):
            world.will(cap, *[rng.choice(["confirm", "confirm", "fail", "unknown", "crash"]) for _ in range(6)])
        self.build(world=world, max_actions=rng.choice([1, 2]), max_rerequest=rng.choice([0, 1]))
        self.provider.queue(*[random_reply(rng, self.c) for _ in range(40)])
        for _ in range(rng.randrange(3, 8)):
            self.due()
            self.runner.run_pending()
        self.runner.run_pending()
        events = list(self.c.store.events())
        types = [e.type for e in events]
        by_key = {e.idempotency_key: e for e in events}
        actions = self.c.state["world_time"].get("world", {}).get("actions", {})
        # 1. исход «подтверждено» возможен только после попытки того же действия, и это событие мира
        for e in events:
            if e.type in ("action.confirmed", "message.out_sent"):
                aid = e.payload["action_id"]
                attempt = by_key.get(f"act:{aid}:try") or by_key.get(f"out:{aid}")
                self.assertIsNotNone(attempt)
                self.assertLess(attempt.seq, e.seq)
                self.assertEqual(e.source.kind, "world")
        # 2. воспоминание о действии — только у подтверждённого и ссылается на подтверждение
        remembered = [e for e in events if e.type == "memory.recorded"]
        confirmed = {e.payload["action_id"] for e in events if e.type in ("action.confirmed", "message.out_sent")}
        self.assertEqual({e.payload["action_id"] for e in remembered}, confirmed)
        for e in remembered:
            rec = self.c.store.get_memory(e.payload["memory_id"])
            ok = by_key.get(f"act:{e.payload['action_id']}:ok") or by_key.get(f"out:{e.payload['action_id']}:ok")
            self.assertEqual(rec.sources, (ok.event_id,))
        # 3. у каждого действия ровно один исход; состояние совпадает с журналом
        for aid, rec in actions.items():
            outcomes = [t for t in types_for(events, aid) if t in OUTCOME_TYPES]
            self.assertLessEqual(len(outcomes), 1, (aid, outcomes))
            self.assertEqual(rec["status"] == "confirmed", aid in confirmed)
        # 4. мир не выполнил ничего сверх попыток; модель событий исхода не создавала
        self.assertEqual(len(self.world.executed), types.count("action.attempted") + types.count("message.out_requested"))
        self.assertTrue(all(e.source.kind != "model" for e in events if e.type.startswith(("action.", "message."))))
        # 5. воспроизведение журнала сходится со снимками
        self.assertEqual(verify(self.c.store), [])


OUTCOME_TYPES = ("action.confirmed", "action.failed", "action.unknown_outcome", "message.out_sent", "message.out_unknown")


def types_for(events, aid):
    return [e.type for e in events if e.payload.get("action_id") == aid]


class MigrationGuardTest(unittest.TestCase):
    """Наследство S06 (R-200): вне storage/backup хранилище открывают только на чтение или с хуком копии перед
    миграцией — иначе формат базы мог бы обновиться без свежей копии."""

    def test_no_bare_store_open_outside_storage_and_backup(self):
        import ast
        from pathlib import Path
        src = Path(__file__).resolve().parents[2] / "src" / "iam"
        bare = []
        for path in sorted(src.rglob("*.py")):
            if path.relative_to(src).parts[0] in ("storage", "backup"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("open", "migrate")
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "Store"):
                    safe = {k.arg for k in node.keywords} & {"before_migrate", "readonly"}
                    if not safe:
                        bare.append(f"{path.relative_to(src)}:{node.lineno}")
        self.assertEqual(bare, [])


if __name__ == "__main__":
    unittest.main()
