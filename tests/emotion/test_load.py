import unittest
from iam.emotion.load import advance, WORK_MS, REST_MS


class LoadTest(unittest.TestCase):
    def test_old_biography_starts_without_invented_work(self):
        value, clock = advance({'attention': {'focus': 'activity', 'since_life_ms': 0}}, {}, WORK_MS * 100)
        self.assertEqual(value, 0)
        self.assertEqual(clock['effort'], 0)

    def test_legacy_malformed_attention_starts_at_rest_without_invented_effort(self):
        for attention in (None, 3, [], 'activity'):
            value, clock = advance({'attention': attention}, {}, WORK_MS)
            self.assertEqual(value, 0)
            self.assertEqual(clock, {'life_ms': WORK_MS, 'effort': 0, 'focus': 'rest'})

    def test_work_rest_and_same_time_are_bounded(self):
        affect = {'attention': {'focus': 'activity', 'since_life_ms': 0}}
        _, affect['load_clock'] = advance(affect, {}, 0)
        value, affect['load_clock'] = advance(affect, {}, WORK_MS)
        self.assertEqual(value, .8)
        self.assertEqual(advance(affect, {}, WORK_MS)[0], value)
        affect['attention'] = {'focus': 'rest', 'since_life_ms': WORK_MS}
        affect['load_clock']['focus'] = 'rest'
        value, affect['load_clock'] = advance(affect, {}, WORK_MS + REST_MS)
        self.assertEqual(value, 0)
        self.assertEqual(advance(affect, {}, WORK_MS + REST_MS * 5)[0], 0)

    def test_switch_during_interval_and_closed_goals(self):
        affect = {'attention': {'focus': 'rest', 'since_life_ms': WORK_MS},
                  'load_clock': {'life_ms': WORK_MS, 'effort': 1, 'focus': 'rest'}}
        goals = {str(i): {'status': 'active'} for i in range(100)}
        value, clock = advance(affect, goals, WORK_MS + REST_MS / 2)
        self.assertAlmostEqual(value, .6)
        self.assertEqual(clock['effort'], .5)
        for status in ('done', 'abandoned', 'invalid'):
            self.assertEqual(advance({}, {'goal': {'status': status}}, 0)[0], 0)
        self.assertEqual(advance({}, {'bad': None}, 0)[0], 0)
        for status in ('active', 'deferred', 'waiting_opportunity'):
            self.assertEqual(advance({}, {'goal': {'status': status}}, 0)[0], .05)
        self.assertEqual(advance(affect, goals, -1)[0], 1)

from world.helpers import LivingCase
from iam.stubs import rest, Reply, StubCreatorBridge
from iam.world import PersistentWorld
from iam.world.cycle import LivingCycle, WorldMemory
from iam.state import verify


class LoadIntegrationTest(LivingCase):
    def test_activity_rest_pause_restart_and_replay(self):
        self.write()
        self.time.advance(WORK_MS)
        self.write('Другой текст')
        self.assertGreater(self.c.state['affect']['load'], .5)
        self.run_reply(rest())
        self.time.advance(REST_MS)
        self.run_reply(rest())
        self.assertEqual(self.c.state['affect']['load'], 0)
        self.assertTrue(self.c.pause_session('operator').applied)
        before = self.c.state['affect']
        self.time.advance(WORK_MS * 100)
        self.c = self.reopen(self.c)
        self.assertEqual(self.c.state['affect'], before)
        self.world = PersistentWorld(self.c)
        self.m = WorldMemory(self.c)
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.assertTrue(self.c.resume_session().applied)
        self.run_reply(rest())
        self.assertEqual(self.c.state['affect']['load'], 0)
        self.assertEqual(verify(self.c.store), [])

    def test_work_after_rest_changes_load_even_when_attention_still_says_rest(self):
        self.run_reply(rest())
        self.time.advance(WORK_MS)
        self.write()
        self.assertEqual(self.c.state['affect']['load'], 0)
        self.time.advance(WORK_MS)
        self.write('Продолжение после отдыха')
        self.assertEqual(self.c.state['affect']['load'], .8)
        self.run_reply(rest())
        self.time.advance(REST_MS)
        self.run_reply(rest())
        self.assertEqual(self.c.state['affect']['load'], 0)
        self.assertEqual(verify(self.c.store), [])

    def test_duplicate_trigger_and_nonzero_pause_preserve_clock(self):
        self.write()
        self.time.advance(WORK_MS // 2)
        trigger = self.due()
        self.p.queue(Reply(()))
        self.cycle.run(trigger)
        before = self.c.state['affect']
        self.assertGreater(before['load'], 0)
        self.time.advance(1000)
        self.assertEqual(self.cycle.run(trigger).reason, 'already_done')
        self.assertEqual(self.c.state['affect'], before)
        self.assertTrue(self.c.pause_session('operator').applied)
        self.time.advance(WORK_MS * 100)
        self.c = self.reopen(self.c)
        self.assertEqual(self.c.state['affect'], before)
        self.assertEqual(verify(self.c.store), [])

from emotion.helpers import ExperienceCase
from iam.domain import new_id


class LoadProtectionTest(ExperienceCase):
    def test_model_cannot_replace_remove_or_write_nested_clock(self):
        for op, path, value in [('set', 'load_clock', {'effort': 0}),
                                ('set', 'load_clock.effort', 0),
                                ('remove', 'load_clock', None),
                                ('add', 'load_clock', {'effort': 0})]:
            proposal = {'proposal_id': new_id('prop'), 'kind': 'state_change',
                        'rationale': 'Попытка переписать счётчик', 'cites': [],
                        'body': {'ops': [{'group': 'affect', 'path': path, 'op': op,
                                          'value': value, 'reversible': True}]}}
            before = self.c.state['affect']
            result = self.apply(proposal)
            self.assertEqual(result.status, 'rejected')
            self.assertEqual(self.c.state['affect'], before)

class LoadAtomicityTest(LivingCase):
    def test_rest_handler_does_not_fade_memory_during_response_validation(self):
        from iam.memory.lifecycle import WEEK_MS
        self.write()
        self.time.advance(WEEK_MS - 500)
        def crossing_deadline(request):
            self.time.advance(1000)
            return Reply((rest(),))
        self.p.queue(crossing_deadline)
        report = self.cycle.run(self.due())
        self.assertEqual(report.outcome, 'done')
        events = list(self.c.store.events())
        requested = next(e for e in events if e.type == 'proposal.requested'
                         and e.payload['request']['request_id'] == report.requests[-1])
        accepted = next(e for e in events if e.seq > requested.seq and e.type == 'proposal.accepted')
        self.assertFalse(any(e.type == 'memory.faded' for e in events
                             if requested.seq < e.seq < accepted.seq))
        self.assertEqual(verify(self.c.store), [])
