import copy
import math
import random

from iam.attention import AttentionPolicy, CHOICES, NOTICE_CAP, unit
from iam.domain import Op
from iam.state import EventSpec
from iam.stubs import Reply, attend, rest

from perception.test_perception import PerceptionCase


class SignalTest(PerceptionCase):
    def test_all_five_choices_and_no_notifications(self):
        for n, focus in enumerate(CHOICES):
            iid, trigger = self.arrive(number=100+n)
            self.run_reply(trigger, attend(focus, iid if focus in ('event', 'dialogue') else None))
            self.assertEqual(self.c.state['affect']['attention']['focus'], focus)
        self.provider.queue(Reply((rest(),)))
        trigger = self.c.find_event(key='rd:' + input_id_for('telegram', '103'))
        self.cycle.run(trigger)
        self.assertEqual(self.c.state['affect']['attention']['focus'], 'rest')

    def test_notification_property_bounded_and_alternatives_remain(self):
        policy = AttentionPolicy()
        state = self.memory.decision_state()
        for count in [0, 1, 2, 100, 1000000, *[random.Random(4+n).randrange(10000000) for n in range(100)]]:
            signal = policy.signals(state, {'count': count})
            doubled = policy.signals(state, {'count': 2*count})
            self.assertGreaterEqual(signal['notice_signal'], 0)
            self.assertLessEqual(doubled['notice_signal'], NOTICE_CAP)
            self.assertEqual(signal['choices'], list(CHOICES))
            self.assertIn('activity', doubled['choices'])
            self.assertIn('rest', doubled['choices'])

    def test_each_feature_changes_trace_and_current_focus_is_retained(self):
        policy = AttentionPolicy()
        state = self.memory.decision_state()
        notice = {'count': 1, 'items': [{'sender': '123'}]}
        base = policy.signals(state, notice)
        for group, key, value, feature in [('affect','load', .8, 'load'),
                ('intentions','int_Task00001', {}, 'goal_signal'),
                ('relations','123', {'closeness': .7}, 'relationship_signal'),
                ('affect','attention', {'focus':'activity','switch_history':[1,2]}, 'switch_cost')]:
            changed = copy.deepcopy(state)
            changed[group][key] = value
            self.assertNotEqual(base[feature], policy.signals(changed, notice)[feature])
        iid, trigger = self.arrive()
        state['affect']['attention'] = {'focus':'activity','ref':'book'}
        self.assertEqual(policy.choose(trigger, state).focus, 'activity')
        self.assertEqual(policy.choose(trigger, state).ref, 'book')
        state['affect']['attention'] = 3
        self.assertIsNone(policy.signals(state, {'count':0})['current_focus'])
        self.assertEqual(policy.choose(trigger, state).focus, 'event')
        state['affect']['attention'] = {'switch_history':3}
        self.assertEqual(policy.signals(state, notice)['switch_cost'], 0)

    def test_numeric_bounds_and_invalid_features(self):
        for value in [True, None, '1', math.nan, math.inf, -math.inf, -1]:
            self.assertEqual(unit(value), 0)
        self.assertEqual(unit(3), 1)
        self.assertEqual(unit(.3), .3)


from iam.cycle.runner import input_id_for
