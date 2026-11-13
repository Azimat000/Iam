import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from iam.social.development import relationship_context
from iam.stubs import rest
from metacognition import test_social_change as fixtures


class RelationshipContextTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SocialChangeTest()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def test_prior_own_relation_reaches_later_choice_with_exact_basis(self):
        source, note = self.f.episode()
        self.f.time.advance(2000)
        self.f.episode('Другой разговор об обычном дне', interpreted=False)
        self.f.run_reply(rest())
        context = self.f.p.calls[-1]['context']
        packets = [json.loads(i['text']) for i in context if 'relationship_experience' in i['text']]
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]['text'], note.text)
        self.assertEqual(packets[0]['sources'], [source.memory_id])
        self.assertEqual(packets[0]['interpretation']['closeness'], 0.5)
        self.assertTrue(any(i['ref'] == note.memory_id for i in context))
        options = [json.loads(i['text']) for i in context if 'social_update_option' in i['text']]
        self.assertTrue(options)
        self.assertIn(options[0]['source'], {i['ref'] for i in context})
        self.assertEqual(options[0]['format']['body']['draft']['content']['social'],
                         {'person': packets[0]['person']})

    def test_no_unread_off_forgotten_revised_or_other_contact_leak(self):
        source, note = self.f.episode()
        trigger = self.f.due()
        visible = [{'ref': source.memory_id}]
        self.assertTrue(relationship_context(self.f.m, visible, trigger))
        self.assertEqual(relationship_context(self.f.m, [], trigger), [])
        self.assertEqual(relationship_context(self.f.m, [{'ref': 'inp_Unread0001'}], trigger), [])
        self.assertTrue(self.f.m.revise(note.memory_id, 'Новый взгляд', sources=note.sources, reason='Уточнение').applied)
        result = relationship_context(self.f.m, visible, trigger)
        self.assertNotIn('relationship_experience', json.dumps(result))
        self.assertNotIn(note.text, json.dumps(result, ensure_ascii=False))
        self.f.m.off = True
        self.assertEqual(relationship_context(self.f.m, visible, trigger), [])
        self.f.m.off = False
        self.assertTrue(self.f.m.fade(source.memory_id).applied)
        self.assertEqual(relationship_context(self.f.m, visible, trigger), [])

    def test_current_option_not_duplicated_and_bounded_safe_context(self):
        source, note = self.f.episode()
        trigger = SimpleNamespace(type='input.read', event_id=source.sources[0])
        visible = [{'ref': source.memory_id}]
        result = relationship_context(self.f.m, visible, trigger)
        self.assertEqual(len(result), 1)
        self.assertNotIn('social_update_option', json.dumps(result))
        self.assertTrue(all(len(i['text']) <= self.f.m.text_limit for i in result))
        with patch.object(self.f.m, '_safe_item', return_value=False):
            self.assertEqual(relationship_context(self.f.m, visible, self.f.due()), [])
        self.f.m.text_limit = 10
        self.assertEqual(relationship_context(self.f.m, visible, self.f.due()), [])
