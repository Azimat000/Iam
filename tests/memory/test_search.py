"""Поиск по естественному вопросу: релевантность, границы и отсутствие готового ответа."""
from types import SimpleNamespace
import unittest

from iam.memory.search import match, relevance, words


class SearchTest(unittest.TestCase):
    def test_normalization_and_conservative_word_forms(self):
        self.assertEqual(words('Я и ёлка, ЕЛКА! the'), {'елка'})
        self.assertEqual(words('Код 7'), {'код', '7'})
        self.assertEqual(match('перчаткой', 'перчатка'), .75)
        self.assertEqual(match('музею', 'музей'), .75)
        self.assertEqual(match('дом', 'домен'), 0)
        self.assertEqual(match('чай', 'случай'), 0)
        self.assertEqual(match('кот', 'кот'), 1)

    def test_partial_question_matches_and_rare_detail_beats_generic_word(self):
        rows = [SimpleNamespace(memory_id=str(i), text=t) for i,t in enumerate([
            'В обсерватории оставили медный компас.', 'Вечером гуляли.', 'Вечером читали.', 'Вечером ужинали.', ''])]
        scores = relevance('Вечером я вспомнил обсерваторию. Что там оставили?', rows)
        self.assertGreater(scores['0'], scores['1'])
        self.assertNotIn('4', scores)
        self.assertEqual(relevance('Я и ты', rows), {})
        self.assertEqual(relevance('самолёт', rows), {})
        self.assertEqual(relevance('компас', []), {})
