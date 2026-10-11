"""Полный проход цикла на заглушках (R-103, R-106): стадии по порядку, события с идентификаторами и версиями."""
import unittest

from iam.cycle import STAGES
from iam.state import replay, verify
from iam.stubs import Reply, act, say

from .helpers import CAP_NOTES, CAP_TG, CycleCase


class FullPassTest(CycleCase):
    def test_all_stages_in_order_and_in_the_journal(self):
        cycle = self.build(Reply((act(CAP_NOTES, {"title": "заметка"}),)))
        trigger = self.due()
        report = cycle.run(trigger)
        self.assertEqual((report.outcome, report.reason), ("done", None), report)
        self.assertEqual(report.names(), list(STAGES))
        self.assertTrue(all(s.status == "ok" for s in report.stages), report.stages)
        events = self.journal(report)
        self.assertEqual([e.type for e in events], [
            "time.moment_due", "attention.chosen", "memory.retrieved", "proposal.requested", "proposal.received",
            "proposal.accepted", "action.attempted", "action.confirmed", "memory.recorded"])
        # у каждого события есть идентификатор, эпоха и версия; версии не убывают
        self.assertTrue(all(e.event_id.startswith("evt_") and e.epoch == 1 for e in events))
        versions = [e.state_version for e in events]
        self.assertEqual(versions, sorted(versions))
        self.assertEqual([s.state_version for s in report.stages], sorted(s.state_version for s in report.stages))
        self.assertEqual(self.c.state_version, versions[-1])
        # все стадии связаны с циклом
        by_type = {e.type: e for e in events}
        self.assertEqual(by_type["attention.chosen"].payload["cycle_id"], report.cycle_id)
        self.assertEqual(by_type["proposal.requested"].payload["request"]["cycle_id"], report.cycle_id)
        self.assertEqual(by_type["action.confirmed"].source.kind, "world")

    def test_confirmed_action_becomes_a_memory_with_the_confirmation_as_source(self):
        cycle = self.build(Reply((act(CAP_NOTES, {"title": "заметка"}),)))
        report = cycle.run(self.due())
        (action,) = report.actions
        self.assertEqual(action.status, "confirmed")
        rec = self.c.store.get_memory(action.memory_id)
        confirmed = self.c.find_event(key=f"act:{action.action_id}:ok")
        self.assertEqual((rec.layer, rec.origin, rec.sources), ("personal", "observation", (confirmed.event_id,)))
        self.assertIn(CAP_NOTES, rec.text)
        self.assertEqual(self.c.state["world_time"]["world"]["actions"][action.action_id]["status"], "confirmed")
        self.assertEqual(self.world.executed[0].capability_id, CAP_NOTES)

    def test_state_equals_replay_after_a_cycle(self):
        cycle = self.build(Reply((say(CAP_TG, "operator", "Привет"),)))
        report = cycle.run(self.due())
        self.assertEqual(report.actions[0].status, "confirmed")
        self.assertEqual(verify(self.c.store), [])
        self.assertEqual(list(replay(self.c.store))[-1].state, self.c.state)


if __name__ == "__main__":
    unittest.main()
