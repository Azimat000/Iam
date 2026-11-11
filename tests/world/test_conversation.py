"""Conversation skips optional preludes while preserving voluntary, private perception."""
import json

from iam.domain import new_id
from iam.state import EventSpec, verify
from iam.stubs import Reply, attend, rest
from iam.world.options import OptionsCycle
from world.helpers import LivingCase
from world import test_prospective


class ConversationTest(LivingCase):
    def enable(self):
        self.read()
        test_prospective.ProspectiveTest.rebuild(self, OptionsCycle)
        self.mode = True
        self.cycle.fast_inputs = lambda: self.mode

    def arrive(self, text='UNREAD_SECRET'):
        iid = new_id('inp')
        event = self.c.record_event(EventSpec('input.arrived', 'rx:test:' + iid,
            {'input_id': iid, 'text': text, 'sender': 'creator', 'channel': 'telegram'})).event
        return iid, event

    def test_arrival_and_read_use_one_choice_each_with_content_only_after_reading(self):
        self.enable()
        iid, event = self.arrive()
        self.p.queue(Reply((attend('dialogue', iid),)))
        count = len(self.p.calls)
        report = self.cycle.run(event)
        self.assertEqual(report.outcome, 'done')
        self.assertEqual(len(self.p.calls), count + 1)
        self.assertEqual(self.p.calls[-1]['purpose'], 'choose_action')
        self.assertNotIn('UNREAD_SECRET', json.dumps(self.p.calls[-1]['context']))
        read_event = next(e for e in self.c.events_after(0) if e.type == 'input.read')
        self.p.queue(Reply((rest(),)))
        self.assertEqual(self.cycle.run(read_event).outcome, 'done')
        self.assertEqual(len(self.p.calls), count + 2)
        self.assertIn('UNREAD_SECRET', json.dumps(self.p.calls[-1]['context']))
        self.assertFalse(any(q['purpose'] == 'appraise' for q in self.p.calls[count:]))
        self.assertEqual(verify(self.c.store), [])

    def test_declining_to_read_stays_private_and_other_triggers_still_appraise(self):
        self.enable()
        _, event = self.arrive()
        self.p.queue(Reply((rest(),)))
        self.assertEqual(self.cycle.run(event).outcome, 'done')
        self.assertNotIn('UNREAD_SECRET', json.dumps(self.p.calls[-1]['context']))
        self.assertFalse(any(e.type == 'input.read' for e in self.c.events_after(0)))
        count = len(self.p.calls)
        self.assertEqual(self.run_reply(replies=(Reply(()), Reply((rest(),)))).outcome, 'done')
        self.assertEqual([q['purpose'] for q in self.p.calls[count:]], ['appraise', 'appraise', 'choose_action'])

    def test_disabling_conversation_restores_appraisal_for_an_incoming_trigger(self):
        self.enable()
        self.mode = False
        _, event = self.arrive()
        count = len(self.p.calls)
        self.p.queue(Reply(()), Reply(()), Reply((rest(),)))
        self.assertEqual(self.cycle.run(event).outcome, 'done')
        self.assertEqual([q['purpose'] for q in self.p.calls[count:]], ['appraise', 'appraise', 'choose_action'])
