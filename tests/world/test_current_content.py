"""Current-turn summaries refer to the longest already-visible compatible window."""
import copy
import json
from types import SimpleNamespace
import unittest

from iam.world.conversation import current_content
from iam.stubs import rest
from world import test_interaction as fixtures


class CurrentContentTest(unittest.TestCase):
    def setUp(self):
        self.trigger = SimpleNamespace(event_id='evt_Read001')
        self.current = {'ref': 'mem_Current01', 'version': 1, 'text': 'hello',
                        'excerpt': {'start': 0, 'end': 5, 'total': 20, 'more': True}}
        self.packet = {'version': 1, 'sources': ['evt_Read001'], 'text': 'hello longer message',
                       'text_window': {'start': 0, 'end': 20, 'total': 20, 'more': False}}

    def item(self, packet=None, **changes):
        return {'ref': 'mem_Current01', 'origin': 'stated_by_other', 'layer': 'personal',
                'text': json.dumps(self.packet if packet is None else packet), **changes}

    def test_pointer_preserves_exact_longest_window_without_copying_or_mutation(self):
        shorter = copy.deepcopy(self.packet)
        shorter['text'] = shorter['text'][:12]
        shorter['text_window'].update(end=12, more=True)
        context = [self.item(shorter), self.item(), self.item(shorter)]
        before = copy.deepcopy(context)
        current_content(self.trigger, context, self.current)
        self.assertEqual(self.current['content_ref'], 'mem_Current01')
        self.assertEqual(self.current['content_window'], self.packet['text_window'])
        self.assertEqual(self.current['text'], 'hello')
        self.assertEqual(context, before)
        current_content(self.trigger, context, None)

    def test_unrelated_stale_unread_partial_and_malformed_windows_are_not_linked(self):
        invalid = [self.item(ref='mem_Other0001'), self.item(layer='archive'), self.item(origin='observation'),
                   self.item(text='bad json'), self.item([])]
        for changes in ({'version': 2}, {'sources': []}, {'text_window': []}, {'text': None}):
            invalid.append(self.item({**self.packet, **changes}))
        for changes in ({'start': 1}, {'end': '20'}, {'end': 19}, {'total': 21}):
            invalid.append(self.item({**self.packet, 'text_window': {**self.packet['text_window'], **changes}}))
        invalid.append(self.item({**self.packet, 'text': 'OTHER longer message'}))
        invalid.append(self.item({**self.packet, 'text': 'hello', 'text_window': {'start': 0, 'end': 5, 'total': 20}}))
        invalid.append(self.item({**self.packet, 'text': 'hello longer message!',
                                  'text_window': {'start': 0, 'end': 21, 'total': 20}}))
        for item in invalid:
            with self.subTest(item=item):
                current = copy.deepcopy(self.current)
                current_content(self.trigger, [item], current)
                self.assertNotIn('content_ref', current)


class CurrentContentIntegrationTest(unittest.TestCase):
    def test_question_after_short_overview_is_linked_in_real_request(self):
        case = fixtures.InteractionTest()
        self.addCleanup(case.doCleanups)
        case.setUp()
        self.addCleanup(case.tearDown)
        text = 'Я долго обдумывал эту историю. ' * 28 + 'Как ты относишься к этой мысли?'
        iid, _ = case.read(text, 1)
        case.run_reply(case.c.find_event(key='rd:' + iid), rest())
        current = case.frame()['read_now']
        context = case.provider.calls[-1]['context']
        personal = next(json.loads(item['text']) for item in context
                        if item['ref'] == current['content_ref'] and item['layer'] == 'personal')
        self.assertNotIn('Как ты относишься', current['text'])
        self.assertIn('Как ты относишься', personal['text'])
        self.assertEqual(personal['text_window'], current['content_window'])
        self.assertEqual(personal['text'], text)
        self.assertLess(len(json.dumps(case.frame(), ensure_ascii=False)), 3000)
