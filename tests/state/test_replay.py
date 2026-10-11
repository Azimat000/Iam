"""Воспроизведение журнала и проверка согласованности: подделки в обход контроллера обнаруживаются."""
import unittest
from dataclasses import replace

from iam.domain import Event, Op, Source, Transition, new_id
from iam.state import EventSpec, ReplayError, replay, verify
from iam.storage import Snapshot

from .helpers import PID, StateCase, memory, plain_event, set_


class ReplayTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)

    def forged(self, c, *, version, ops=(set_("traits", "forged", 1),), changes=None, epoch=1, key=None,
               changes_state=True, life=None, companion=False):
        """Событие-носитель перехода, записанное напрямую в хранилище (в обход контроллера)."""
        t = Transition(new_id("trn"), PID, epoch, version - 1, tuple(ops), "подделка", (self.root(c),))
        payload = {} if companion else {"transition": t.to_dict(), "changes": changes if changes is not None else []}
        return Event(event_id=new_id("evt"), persona_id=PID, type="state.transition_applied",
                     occurred_at="2026-10-11T10:00:00.000Z", life_time_ms=c.store.get_event(c.store.last_seq()).life_time_ms
                     if life is None else life, source=Source("core", "forger"),
                     idempotency_key=key or f"trn:{t.transition_id}", epoch=epoch, state_version=version,
                     changes_state=changes_state, payload=payload)

    def test_replay_matches_snapshots_step_by_step(self):
        c = self.c
        for i in range(4):
            c.apply(self.prop(c, set_("beliefs", f"b{i}", i), Op("affect", "reactions", "add", i, True)))
        c.apply(self.prop(c, Op("memory", "mem_AAAAAAAA", "add", memory(), True)))
        c.pause_session()
        steps = list(replay(c.store))
        self.assertEqual([s.state_version for s in steps], list(range(1, c.state_version + 1)))
        for s in steps:
            self.assertEqual(s.state, c.store.get_snapshot(1, s.state_version).state, f"версия {s.state_version}")
            self.assertEqual(s.problems, [])
        self.assertEqual(steps[-1].state, c.state)
        self.assertEqual(list(steps[-1].memory), ["mem_AAAAAAAA"])
        self.assertEqual(verify(c.store), [])

    def test_state_rebuilt_after_restart_equals_snapshot(self):
        c = self.c
        c.apply(self.prop(c, set_("beliefs", "x", {"a": [1, 2]})))
        c.pause_session()
        c2 = self.reopen(c)
        self.assertEqual(c2.state, list(replay(c2.store))[-1].state)

    def test_forged_snapshot_is_detected(self):
        c = self.c
        v = c.state_version
        ev = self.forged(c, version=v + 1)
        stored, _ = c.store.append_event(ev)
        c.store.put_snapshot(Snapshot(1, v + 1, stored.seq, ev.life_time_ms, ev.occurred_at, {**c.state, "traits": {"evil": 1}}))
        problems = verify(c.store)
        self.assertTrue(any("не совпадает с воспроизведённым состоянием" in p for p in problems), problems)
        self.assertTrue(any("записанные изменения не совпадают" in p for p in problems), problems)

    def test_missing_snapshot_and_wrong_final_snapshot(self):
        c = self.c
        v = c.state_version
        c.store.append_event(self.forged(c, version=v + 1))
        problems = verify(c.store)
        self.assertIn(f"нет снимка версии {v + 1}", problems)
        self.assertIn("последний снимок не соответствует последнему переходу", problems)

    def test_version_jump_and_wrong_base_are_detected(self):
        c = self.c
        v = c.state_version
        c.store.append_event(self.forged(c, version=v + 5))
        problems = verify(c.store)
        self.assertTrue(any(f"версия {v + 5}, ожидалась {v + 1}" in p for p in problems), problems)
        self.assertTrue(any("переход основан на" in p for p in problems), problems)

    def test_companion_with_other_version_is_detected(self):
        c = self.c
        v = c.state_version
        c.store.append_event(replace(plain_event(0), type="proposal.accepted", changes_state=True,
                                     idempotency_key="prop:prop_FORGED001:accept", state_version=v + 3,
                                     life_time_ms=c.store.get_event(c.store.last_seq()).life_time_ms))
        self.assertTrue(any("событие перехода с версией" in p for p in verify(c.store)))

    def test_decreasing_life_time_is_detected(self):
        c = self.c
        self.time.advance(1000)
        c.record_event(EventSpec("input.arrived", "rx:tg:1"))
        c.store.append_event(plain_event(1, n=2))      # событие «из прошлого»
        self.assertTrue(any("время жизни убывает" in p for p in verify(c.store)))

    def test_epoch_change_needs_restore_slice(self):
        c = self.c
        ev = replace(plain_event(0), epoch=2, idempotency_key="rx:tg:epoch2",
                     life_time_ms=c.store.get_event(c.store.last_seq()).life_time_ms)
        c.store.append_event(ev)
        with self.assertRaises(ReplayError):
            list(replay(c.store))
        self.assertTrue(any("смена эпохи" in p for p in verify(c.store)))

    def test_memory_tampering_is_detected(self):
        c = self.c
        c.apply(self.prop(c, Op("memory", "mem_AAAAAAAA", "add", memory("чай"), True)))
        self.assertEqual(verify(c.store), [])
        from iam.domain import MemoryRecord
        c.store.update_memory(MemoryRecord.from_dict(memory("кофе", version=2)))     # в обход контроллера
        self.assertTrue(any("mem_AAAAAAAA" in p and "не совпадает" in p for p in verify(c.store)))

    def test_missing_memory_record_is_detected(self):
        c = self.c
        t = Transition(new_id("trn"), PID, 1, c.state_version, (Op("memory", "mem_GHOSTGHOST", "add", memory(mid="mem_GHOSTGHOST"), True),),
                       "память без записи", (self.root(c),))
        ev = replace(self.forged(c, version=c.state_version + 1), payload={"transition": t.to_dict(), "changes": [
            {"group": "memory", "path": "mem_GHOSTGHOST", "op": "add", "before": None, "after": memory(mid="mem_GHOSTGHOST")}]})
        c.store.append_event(ev)
        self.assertTrue(any("mem_GHOSTGHOST" in p and "отсутствует" in p for p in verify(c.store)))

    def test_wrong_at_seq_is_detected(self):
        c = self.c
        v = c.state_version
        ev = self.forged(c, version=v + 1, ops=(set_("traits", "late", 1),),
                         changes=[{"group": "traits", "path": "late", "op": "set", "before": None, "after": 1}])
        stored, _ = c.store.append_event(ev)
        c.store.append_event(replace(plain_event(0, n=3), type="proposal.accepted", changes_state=True,
                                     idempotency_key="prop:prop_LATEONE01:accept", state_version=v + 1,
                                     life_time_ms=ev.life_time_ms))
        state = c.state
        state["traits"]["late"] = 1
        c.store.put_snapshot(Snapshot(1, v + 1, stored.seq, ev.life_time_ms, ev.occurred_at, state))
        self.assertTrue(any("at_seq" in p for p in verify(c.store)), verify(c.store))


class EmptyJournalTest(StateCase):
    def test_empty_journal_has_nothing_to_replay(self):
        c = self.controller(created=False)
        self.assertEqual(verify(c.store), [])
        self.assertEqual(list(replay(c.store)), [])

    def test_snapshot_without_transitions_is_a_problem(self):
        c = self.controller(created=False)
        c.record_event(EventSpec("input.arrived", "rx:tg:1"))
        c.store.put_snapshot(Snapshot(1, 1, 1, 0, "2026-10-11T10:00:00.000Z", {}))
        self.assertIn("снимок есть, а переходов в журнале нет: версия 1", verify(c.store)[0])


if __name__ == "__main__":
    unittest.main()
