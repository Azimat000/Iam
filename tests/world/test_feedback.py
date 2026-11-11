"""A rejected proposal is an observed failure, never a delivered message or an automatic retry."""
import json
from unittest.mock import patch

from iam.domain import Op, new_id
from iam.interfaces import Capability
from iam.state import EventSpec, verify
from iam.stubs import Reply, StubWorld, act, rest, say
from iam.world import WRITE
from world.helpers import LivingCase


class FeedbackTest(LivingCase):
    def feedback(self):
        return [json.loads(row['text']) for row in self.p.calls[-1]['context']
                if row['text'].startswith('{"kind": "proposal_feedback"')]

    def reject_message(self, text='REJECTED_PRIVATE_PAYLOAD'):
        self.caps.register(Capability('cap_Telegram01', 'Telegram'))
        self.world.external = getattr(self, 'external', None) or StubWorld()
        self.external = self.world.external
        message = say('cap_Telegram01', '123', text)
        report = self.run_reply(
            act(WRITE, {'project_id': 'note', 'title': 'Запись', 'text': 'Свой текст'}), message)
        self.assertEqual(len(report.actions), 1)
        self.assertEqual(report.actions[0].status, 'confirmed')
        event = next(e for e in self.c.store.events()
                     if e.type == 'proposal.rejected' and e.payload['proposal_id'] == message['proposal_id'])
        self.assertEqual(event.payload['reason'], 'too_many_actions')
        return event

    def test_partial_rejection_reaches_next_choice_but_does_not_send_by_itself(self):
        rejected = self.reject_message()
        self.assertEqual(self.external.executed, [])
        self.assertEqual(self.run_reply(rest()).outcome, 'done')
        self.assertEqual(self.feedback(), [{'kind': 'proposal_feedback', 'status': 'rejected',
                                           'proposal_kind': 'message', 'reason': 'too_many_actions',
                                           'executed': False}])
        self.assertIn(rejected.event_id, [row['ref'] for row in self.p.calls[-1]['context']])
        self.assertNotIn('REJECTED_PRIVATE_PAYLOAD', json.dumps(self.p.calls[-1]['context']))
        self.assertEqual(self.external.executed, [])  # Even after a further choice, no automatic retry.

        def choose_message(request):
            self.assertIn('proposal_feedback', json.dumps(request['context']))
            return Reply((say('cap_Telegram01', '123', 'Новое выбранное сообщение'),))
        self.p.queue(choose_message)
        report = self.cycle.run(self.due())
        self.assertEqual(report.actions[0].status, 'confirmed')
        self.assertEqual([item.text for item in self.external.executed], ['Новое выбранное сообщение'])
        self.assertEqual(verify(self.c.store), [])

    def test_only_three_recent_rejections_in_the_current_session_are_visible(self):
        events = [self.reject_message(str(number)) for number in range(5)]
        self.run_reply(rest())
        refs = [row['ref'] for row in self.p.calls[-1]['context']
                if row['text'].startswith('{"kind": "proposal_feedback"')]
        self.assertEqual(refs, [event.event_id for event in events[-3:]])
        self.c.stop_session()
        self.c.start_session()
        self.run_reply(rest())
        self.assertEqual(self.feedback(), [])

    def test_memory_off_and_missing_session_start_do_not_recover_rejection_history(self):
        self.reject_message()
        self.m.off = True
        self.run_reply(rest())
        self.assertEqual(self.feedback(), [])
        self.m.off = False
        with patch.object(self.c, 'events_after', return_value=[]):
            self.assertEqual(self.cycle._proposal_feedback(), [])

    def test_feedback_never_exposes_unread_or_forgotten_payloads(self):
        source = self.write('FORGOTTEN_WORK_PAYLOAD')
        self.assertTrue(self.m.fade(source.memory_id).applied)
        self.c.record_event(EventSpec('input.arrived', 'rx:test:hidden', {
            'input_id': 'inp_UnreadExample01', 'channel': 'telegram', 'sender': 'private',
            'text': 'UNREAD_INCOMING_PAYLOAD'}))
        self.reject_message('REJECTED_MESSAGE_PAYLOAD')
        self.run_reply(rest())
        context = json.dumps(self.p.calls[-1]['context'])
        self.assertEqual(len(self.feedback()), 1)
        for hidden in ('FORGOTTEN_WORK_PAYLOAD', 'UNREAD_INCOMING_PAYLOAD', 'REJECTED_MESSAGE_PAYLOAD'):
            self.assertNotIn(hidden, context)

    def test_transition_rejections_without_kind_do_not_break_the_next_choice(self):
        expected = self.reject_message()
        for _ in range(4):
            proposal = self.prop(self.c, Op('traits', 'missing', 'remove'), actor='model',
                                 proposal_id=new_id('prop'), cause=self.root(self.c))
            result = self.c.apply(proposal)
            self.assertEqual(result.reason, 'reflection_required')
            event = self.c.store.find_event(key=f'prop:{proposal.transition.proposal_id}:reject')
            self.assertNotIn('kind', event.payload)
        for body in ({'kind': 'message', 'reason': None}, {'kind': 'unsupported', 'reason': 'invalid'}):
            pid = new_id('prop')
            self.c.record_event(EventSpec('proposal.rejected', f'prop:{pid}:reject',
                                          {'proposal_id': pid, **body}))
        self.assertEqual(self.run_reply(rest()).outcome, 'done')
        refs = [row['ref'] for row in self.p.calls[-1]['context']
                if row['text'].startswith('{"kind": "proposal_feedback"')]
        self.assertEqual(refs, [expected.event_id])
        self.assertEqual(verify(self.c.store), [])
