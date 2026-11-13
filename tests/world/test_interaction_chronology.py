"""A late opening cannot silently promote an old closing into the newest exchange."""
import copy
import json
from types import SimpleNamespace
import unittest

from iam.social import SocialMemory
from iam.clock import life_moment
from iam.domain import new_id
from iam.stubs import StubCapabilities, attend, rest
from iam.world import PersistentWorld, CAPABILITIES
from iam.world.cycle import LivingCycle
from iam.world.conversation import conversation_heads, interaction_context
from perception.test_perception import PerceptionCase


def turn(ref, received, person='creator', **extra):
    value = {'kind': 'dialogue_turn', 'direction': 'incoming', 'speaker': 'interlocutor',
             'outcome': 'read', 'content_available': True, 'person': person, 'received_seq': received,
             'seq': 100, 'version': 1, 'text': 'Known words', 'current_read': False,
             'excerpt': {'start': 0, 'end': 11, 'total': 11, 'more': False}, **extra}
    return ref, value


def frame(turns):
    context = [{'ref': ref, 'origin': 'stated_by_other', 'layer': 'state',
                'text': json.dumps(value, ensure_ascii=False)} for ref, value in turns]
    trigger = SimpleNamespace(type='input.read', event_id='evt_Trigger01')
    return json.loads(interaction_context(trigger, context)[-1]['text'])


class ChronologyBoundaryTest(unittest.TestCase):
    def test_heads_compare_receiving_order_per_contact_without_mutation(self):
        values = [turn('mem_Newest01', 20), turn('mem_Oldest01', 10),
                  turn('mem_Other001', 200, 'another'), turn('mem_Middle01', 15),
                  turn('mem_Other002', 201, 'another')]
        before = copy.deepcopy(values)
        self.assertEqual(conversation_heads(values), {
            'creator': {'ref': 'mem_Newest01', 'received_seq': 20},
            'another': {'ref': 'mem_Other002', 'received_seq': 201}})
        self.assertEqual(values, before)
        self.assertNotIn('text', json.dumps(conversation_heads(values)))

    def test_unknown_unread_outgoing_and_unavailable_are_not_heads(self):
        changes = [dict(direction='outgoing'), dict(speaker='self'), dict(outcome='unread'),
                   dict(content_available=False), dict(person=None), dict(person=''),
                   dict(received_seq=None), dict(received_seq=True)]
        for changed in changes:
            with self.subTest(changed=changed):
                value = turn('mem_Unknown1', 20)[1]
                value.update(changed)
                self.assertEqual(conversation_heads([('mem_Unknown1', value)]), {})
        self.assertEqual(conversation_heads([]), {})

    def test_old_current_read_points_only_to_newer_visible_same_person(self):
        old = turn('mem_Oldest01', 10, current_read=True, latest_in_direction=True)
        new = turn('mem_Newest01', 20)
        self.assertEqual(frame([new, old])['read_now']['older_than_ref'], new[0])
        self.assertEqual(frame([new, old])['latest_read_ref'], old[0])
        for other in (turn('mem_Other001', 200, 'another'), turn('mem_Earlier1', 5),
                      turn('mem_Equal001', 10)):
            self.assertNotIn('older_than_ref', frame([old, other])['read_now'])

    def test_unknown_current_order_does_not_invent_backlog_relation(self):
        for changed in (dict(received_seq=None), dict(received_seq=True), dict(person='unknown'),
                        dict(person=None), dict(person={}), dict(person=[])):
            old = turn('mem_Oldest01', 10, current_read=True)[1]
            old.update(changed)
            self.assertNotIn('older_than_ref', frame([turn('mem_Newest01', 20),
                                                     ('mem_Oldest01', old)])['read_now'])
        for field in ('person', 'received_seq'):
            old = turn('mem_Oldest01', 10, current_read=True)[1]
            del old[field]
            self.assertNotIn('older_than_ref', frame([turn('mem_Newest01', 20),
                                                     ('mem_Oldest01', old)])['read_now'])


class ChronologyIntegrationTest(PerceptionCase):
    def setUp(self):
        super().setUp()
        self.memory = SocialMemory(self.c)
        world = PersistentWorld(self.c, external=self.world)
        world.initialize()
        self.cycle = LivingCycle(self.c, self.provider, self.memory, world,
                                 StubCapabilities(CAPABILITIES), self.creator)
        self.p = self.cycle.perception

    def test_old_closing_read_after_new_exchange_keeps_new_receiving_anchor(self):
        old, arrived = self.arrive('На сегодня хватит.', 1)
        new, later = self.arrive('Вернулся: продолжим другую тему.', 2)
        self.run_reply(later, attend('dialogue', new))
        self.run_reply(arrived, attend('dialogue', old))
        read = self.c.find_event(key='rd:' + old)
        self.run_reply(read, rest())
        context = self.provider.calls[-1]['context']
        result = json.loads(context[-1]['text'])
        latest = result['conversation_heads']['creator']
        self.assertEqual(latest['received_seq'], later.seq)
        self.assertEqual(result['read_now']['received_seq'], arrived.seq)
        self.assertEqual(result['read_now']['older_than_ref'], latest['ref'])
        self.assertNotEqual(result['latest_read_ref'], latest['ref'])
        # A free choice keeps chronology without inventing a new incoming message.
        free = SimpleNamespace(type='time.moment_due', event_id=read.event_id)
        without_current = [dict(item, text=item['text'].replace('"current_read": true',
                           '"current_read": false')) for item in context[:-1]]
        autonomous = json.loads(interaction_context(free, without_current)[-1]['text'])
        self.assertIsNone(autonomous['read_now'])
        self.assertEqual(autonomous['conversation_heads'], result['conversation_heads'])
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 3000)

    def test_unread_or_faded_newer_message_is_never_used_as_an_anchor(self):
        old, arrived = self.read('Доступная история.', 1)
        hidden, later = self.arrive('НЕПРОЧИТАННЫЙ_СЕКРЕТ', 2)
        self.run_reply(later, rest())
        result = json.loads(self.provider.calls[-1]['context'][-1]['text'])
        self.assertEqual(result['conversation_heads']['creator']['received_seq'], arrived.seq)
        self.assertNotIn('НЕПРОЧИТАННЫЙ_СЕКРЕТ', json.dumps(self.provider.calls[-1]))
        moment = life_moment(new_id('mom'), self.c.life_time_ms, 'chronology_read')
        self.assertTrue(self.c.schedule_moment(moment).applied)
        trigger = self.c.fire_due_moments()[0].events[0]
        self.run_reply(trigger, attend('dialogue', hidden))
        read = self.c.find_event(key='rd:' + hidden)
        record = next(r for r in self.memory.records() if read.event_id in r.sources)
        self.assertTrue(self.memory.fade(record.memory_id).applied)
        self.run_reply(read, rest())
        context = self.provider.calls[-1]['context']
        self.assertNotIn(record.memory_id, json.dumps(json.loads(context[-1]['text'])['conversation_heads']))
        self.assertNotIn('НЕПРОЧИТАННЫЙ_СЕКРЕТ', json.dumps(context))
