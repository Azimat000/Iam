import json

from iam.cycle import Cycle
from iam.social import SocialMemory
from iam.stubs import Reply, StubCapabilities, attend, rest

from perception.test_perception import PerceptionCase


class SocialHistoryTest(PerceptionCase):
    def test_only_read_and_available_experience_is_used(self):
        memory = SocialMemory(self.c)
        self.cycle = Cycle(self.c, self.provider, memory, self.world, StubCapabilities(), self.creator)
        iid, trigger = self.arrive("Разговор о книге")
        self.assertEqual(memory.history()["creator"]["history"], [])
        self.assertEqual(self.run_reply(trigger, attend("dialogue", iid)).outcome, "done")
        record = memory.records()[0]
        self.assertEqual(memory.history()["creator"]["history"], [record.memory_id])
        self.assertEqual(memory.overview()["people"]["creator"]["history"], [record.memory_id])
        self.assertNotIn("Разговор о книге", json.dumps(self.provider.calls[0], ensure_ascii=False))
        read = self.c.find_event(key="rd:" + iid)
        self.provider.queue(Reply((rest(),)))
        self.cycle.run(read)
        self.assertTrue(any(json.loads(entry["text"]).get("history") == [record.memory_id]
                            for entry in self.provider.calls[-1]["context"] if entry["text"].startswith("{")))
        self.assertTrue(memory.fade(record.memory_id).applied)
        self.assertEqual(memory.history()["creator"]["history"], [])
        memory.off = True
        self.assertEqual(memory.history()["creator"]["history"], [])

    def test_old_read_episode_restores_profile_after_reopening(self):
        self.read("Ранее прочитанный разговор")
        ctl = self.reopen(self.c)
        memory = SocialMemory(ctl)
        profile = memory.history()["creator"]
        self.assertEqual(profile["role"], "operator_interlocutor")
        self.assertEqual(len(profile["history"]), 1)
        self.assertIn("last_contact_calendar", profile)
