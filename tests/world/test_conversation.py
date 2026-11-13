"""Conversation skips optional preludes while preserving voluntary, private perception."""
import json
from unittest.mock import patch

from iam.domain import new_id
from iam.state import EventSpec, verify
from iam.stubs import Reply, attend, rest, say, act
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
        self.assertEqual(self.p.calls[-1]['allowed_proposals'], ['attend','no_action'])
        self.assertEqual(self.p.calls[-1]['allowed_actions'], [])
        self.assertNotIn('Короткая история',json.dumps(self.p.calls[-1]['context'],ensure_ascii=False))
        self.assertNotIn('UNREAD_SECRET', json.dumps(self.p.calls[-1]['context']))
        read_event = next(e for e in self.c.events_after(0) if e.type == 'input.read')
        self.p.queue(Reply((rest(),)))
        self.assertEqual(self.cycle.run(read_event).outcome, 'done')
        self.assertEqual(len(self.p.calls), count + 2)
        self.assertIn('UNREAD_SECRET', json.dumps(self.p.calls[-1]['context']))
        self.assertIn('message',self.p.calls[-1]['allowed_proposals'])
        self.assertIn('action',self.p.calls[-1]['allowed_proposals'])
        self.assertFalse(any(q['purpose'] == 'appraise' for q in self.p.calls[count:]))
        self.assertEqual(verify(self.c.store), [])

    def test_notice_rejects_old_reply_or_work_and_keeps_decline_voluntary(self):
        self.enable()
        self.p.compose_message=self.p.propose
        for proposal in (say('cap_Telegram01','creator','Ответ на старую тему'),act('cap_Project01',{})):
            _,trigger=self.arrive()
            before=len(self.p.calls)
            self.p.queue(Reply((proposal,)))
            report=self.cycle.run(trigger)
            self.assertEqual(report.outcome,'rejected')
            self.assertFalse(report.actions)
            self.assertEqual(len(self.p.calls),before+1)
            self.assertFalse(self.cycle._incoming_notice)
        _,trigger=self.arrive()
        self.p.queue(Reply((rest('Сейчас не хочу читать'),)))
        self.assertEqual(self.cycle.run(trigger).outcome,'done')
        self.assertFalse([e for e in self.c.events_after(0) if e.type=='input.read'])

    def test_notice_does_not_apply_to_appraisal_and_flag_clears_on_interruption(self):
        from iam.domain import new_id
        self.enable()
        self.cycle._incoming_notice=True
        request=self.cycle._request(new_id('cyc'),[],0,None,purpose='appraise')
        self.assertIn('message',request['allowed_proposals'])
        self.cycle._incoming_notice=False
        _,trigger=self.arrive()
        self.p.queue(Reply(error=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            self.cycle.run(trigger)
        self.assertFalse(self.cycle._incoming_notice)

    def test_n1_notification_with_memory_disabled_reveals_only_allowed_notice(self):
        self.enable()
        self.m.off=True
        self.cycle.perception.level='N1'
        _,trigger=self.arrive('PRIVATE_UNREAD_BODY')
        self.p.queue(Reply((rest(),)))
        with patch.object(self.m,'retrieve',side_effect=AssertionError('No retrieval before choosing to read')):
            self.assertEqual(self.cycle.run(trigger).outcome,'done')
        context=self.p.calls[-1]['context']
        self.assertTrue(all(i['layer']=='notice' for i in context))
        self.assertEqual(json.loads(context[1]['text']),{'notice_level':'N1','count':1})
        self.assertNotIn('PRIVATE_UNREAD_BODY',json.dumps(context))
        self.assertNotIn('Короткая история',json.dumps(context,ensure_ascii=False))
        self.assertEqual(verify(self.c.store),[])

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
