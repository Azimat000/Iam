import copy
import json
import unittest

from iam.world.realization import realization_context
from iam.providers.codex.presentation import present_context, restore_context


class RealizationTest(unittest.TestCase):
    def test_selected_sources_keep_author_and_citation_without_old_retelling(self):
        def item(ref, layer, data):
            return {'ref': ref, 'origin': 'observation', 'layer': layer,
                    'text': json.dumps(data, ensure_ascii=False)}
        current = item('current', 'state', {'kind': 'dialogue_turn', 'text': 'А теперь?', 'speaker': 'interlocutor'})
        frame = item('current', 'state', {'kind': 'current_interaction', 'read_now': {'ref': 'current'},
            'unread_targets': [], 'last_outgoing': {'ref': 'old', 'text': 'Ошибочный пересказ',
                                                  'excerpt': {'start': 0}, 'outcome': 'confirmed'}})
        old = item('old', 'state', {'kind': 'conversation_episode', 'text': 'Ошибочный пересказ'})
        personal = item('old', 'personal', {'statement_scope': 'utterance_only', 'text': 'Ошибочный пересказ'})
        trait = item('trait', 'state', {'kind': 'character', 'value': 'independent'})
        note = {'ref': 'note', 'origin': 'own_hypothesis', 'layer': 'personal', 'text': 'Мой приобретённый способ'}
        other = item('other', 'personal', [])
        empty_frame = item('current', 'state', {'kind': 'current_interaction', 'last_outgoing': None})
        context = [current, frame, old, personal, trait, note, other, empty_frame]
        readings = [item('current', 'interpretation', {'kind': 'conversation_reading', 'findings': [
            {'scope': scope, 'evidence': [{'ref': ref, 'speaker': speaker, 'quote': quote}]}],
            'semantic_verified': False, 'uncertainty': ''}) for scope, ref, speaker, quote in (
                ('interlocutor_statement', 'correction', 'interlocutor', 'Теперь синий.'),
                ('own_preference', 'old', 'self', 'А мне нравится зелёный.'))]
        before = copy.deepcopy((context, readings))
        result = realization_context(context, readings)
        self.assertEqual((context, readings), before)
        self.assertNotIn('Ошибочный пересказ', json.dumps(result, ensure_ascii=False))
        for kept in (current, trait, note, other, empty_frame):
            self.assertIn(kept, result)
        self.assertEqual(json.loads(result[1]['text'])['last_outgoing'], {'ref': 'old', 'outcome': 'confirmed'})
        quotes = [i for i in result if '"conversation_quote"' in i['text']]
        self.assertEqual([q['ref'] for q in quotes], ['correction', 'old'])
        self.assertEqual([q['origin'] for q in quotes], ['stated_by_other', 'observation'])
        self.assertEqual(restore_context(present_context(result)), result)
        unchanged = realization_context(context, [])
        self.assertEqual(unchanged, context)
        unchanged[0]['text'] = 'changed'
        self.assertEqual((context, readings), before)

    def test_no_findings_does_not_invent_evidence(self):
        reading = {'ref': 'current', 'text': '{"findings": []}'}
        self.assertEqual(realization_context([], [reading]), [reading])
