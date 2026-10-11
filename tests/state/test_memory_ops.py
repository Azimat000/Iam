"""Операции над записями памяти внутри перехода: память меняется в той же версионной последовательности."""
import unittest

from iam.domain import Op
from iam.state import verify
from iam.storage import NotFound, Store

from .helpers import EXP, StateCase, memory


def mop(path, kind, value=None, has=True):
    return Op("memory", path, kind, value, has)


class MemoryOpsTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)

    def test_add_and_set_in_one_transition(self):
        c = self.c
        v = c.state_version
        res = c.apply(self.prop(c, mop("mem_AAAAAAAA", "add", memory("чай")), mop("mem_AAAAAAAA", "set",
                                                                              memory("чай с мятой", version=2))))
        self.assertTrue(res.applied, res.violations)
        self.assertEqual(c.state_version, v + 1, "память меняется той же версией, что и состояние")
        rec = c.store.get_memory("mem_AAAAAAAA")
        self.assertEqual((rec.text, rec.version), ("чай с мятой", 2))
        self.assertEqual(res.changes[0]["before"], None)
        self.assertEqual(res.changes[1]["before"]["version"], 1)
        self.assertEqual([m.memory_id for m in c.store.search_memory("мяты")], [])   # морфологии нет
        self.assertEqual(len(c.store.search_memory("мятой")), 1)
        self.assertEqual(verify(c.store), [])

    def test_set_replaces_existing_record_from_earlier_transition(self):
        c = self.c
        c.apply(self.prop(c, mop("mem_AAAAAAAA", "add", memory("кофе"))))
        res = c.apply(self.prop(c, mop("mem_AAAAAAAA", "set", memory("кофе и чай", version=2))))
        self.assertTrue(res.applied, res.violations)
        self.assertEqual(c.store.get_memory("mem_AAAAAAAA").text, "кофе и чай")
        self.assertEqual(verify(c.store), [])

    def test_rejections_leave_memory_and_state_untouched(self):
        c = self.c
        c.apply(self.prop(c, mop("mem_AAAAAAAA", "add", memory("чай", version=2))))
        cases = {
            "memory_exists": mop("mem_AAAAAAAA", "add", memory("чай")),
            "memory_missing": mop("mem_BBBBBBBB", "set", memory("нет", mid="mem_BBBBBBBB")),
            "memory_immutable": mop("mem_AAAAAAAA", "set", memory("чай", layer="interpretation", version=2)),
            "memory_immutable2": mop("mem_AAAAAAAA", "set", memory("чай", version=1)),   # версия не убывает
            "memory_id_mismatch": mop("mem_CCCCCCCC", "add", memory("x", mid="mem_DDDDDDDD")),
            "wrong_persona": mop("mem_EEEEEEEE", "add", memory("x", mid="mem_EEEEEEEE", persona=EXP)),
            "archive_write": mop("mem_FFFFFFFF", "add", memory("x", mid="mem_FFFFFFFF", layer="archive")),
            "simulation_in_permanent": mop("mem_GGGGGGGG", "add", memory("x", mid="mem_GGGGGGGG",
                                                                        origin="simulation_event")),
            "memory_op_unsupported": mop("mem_AAAAAAAA", "remove", None, False),
            "memory_op_unsupported2": mop("mem_AAAAAAAA.deep", "set", memory("x")),
            "memory_op_unsupported3": mop("mem_AAAAAAAA", "set", None, False),
            "bad_memory": mop("mem_HHHHHHHH", "add", {"memory_id": "mem_HHHHHHHH"}),
        }
        base_state, base_mem = c.state, c.store.get_memory("mem_AAAAAAAA")
        for name, op in cases.items():
            res = c.apply(self.prop(c, op, Op("traits", "z", "set", 1, True)))
            self.assertEqual(res.reason, name.rstrip("0123456789"),
                             f"{name}: {res.violations}")
        self.assertEqual(c.state, base_state)
        self.assertEqual(c.store.get_memory("mem_AAAAAAAA"), base_mem)
        with self.assertRaises(NotFound):
            c.store.get_memory("mem_BBBBBBBB")

    def test_model_cannot_change_memory_by_operations(self):
        c = self.c
        res = c.apply(self.prop(c, mop("mem_AAAAAAAA", "add", memory("чай")), actor="model"))
        self.assertEqual(res.reason, "memory_via_proposal")

    def test_experimental_store_accepts_simulation_events(self):
        store = Store.create(self.tmp / "e.db", EXP)
        self.addCleanup(store.close)
        from iam.state import StateController
        c = StateController(store, clock=self.clock())
        c.create_persona()
        c.start_session()
        rec = memory("в симуляции", persona=EXP, origin="simulation_event", mid="mem_SIMULATE1")
        self.assertTrue(c.apply(self.prop(c, mop("mem_SIMULATE1", "add", rec))).applied)


if __name__ == "__main__":
    unittest.main()
