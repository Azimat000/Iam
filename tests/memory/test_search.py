"""Поиск по естественному вопросу: релевантность, границы и отсутствие готового ответа."""
from types import SimpleNamespace
import unittest

from iam.memory.search import match, relevance, words, diverse


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

    def test_close_retellings_leave_room_for_another_relevant_detail(self):
        rows = {str(i): SimpleNamespace(memory_id=str(i), text=t) for i,t in enumerate([
            'Экспедиция корабль карта отправление',
            'Экспедиция корабль карта отправление сегодня',
            'Карта отмечает старый маяк'])}
        scores = relevance('экспедиция корабль карта', rows.values())
        self.assertEqual(diverse(['0','1','2'], scores, rows, 2), ['0','2'])
        self.assertEqual(diverse(['1','0','2'], scores, rows, 1), ['1'])
        self.assertEqual(diverse([], {}, {}, 5), [])
        self.assertEqual(diverse(['0'], scores, rows, 0), [])

    def test_empty_associated_records_keep_stable_order(self):
        rows = {key:SimpleNamespace(text='') for key in ('first','second')}
        self.assertEqual(diverse(list(rows), {}, rows, 5), list(rows))

    def test_opposite_statements_and_different_numbers_are_not_redundant(self):
        for a,b in [('Я выбрал перчатку', 'Я не выбрал перчатку'),
                    ('Встреча 3 марта', 'Встреча 5 марта')]:
            rows = {'a':SimpleNamespace(text=a), 'b':SimpleNamespace(text=b),
                    'c':SimpleNamespace(text='Другое слабое совпадение')}
            self.assertEqual(diverse(list(rows), {'a':10,'b':9,'c':2}, rows, 2), ['a','b'])

    def test_different_authors_and_delivery_outcomes_are_not_redundant(self):
        for field,left,right in [('origin','observation','stated_by_other'),
                                 ('independence_group','alice','bob'),
                                 ('outcome','confirmed','unknown'), ('outcome','failed','confirmed'),
                                 ('person','alice','bob')]:
            rows = {key:SimpleNamespace(text='Перчатку выбрали для зала',content={}) for key in ('a','b')}
            for key,value in [('a',left),('b',right)]:
                if field=='outcome': rows[key].content['outcome']=value
                elif field=='person': rows[key].content['communication']={'person':value}
                else: setattr(rows[key],field,value)
            rows['c']=SimpleNamespace(text='Слабое совпадение',content={})
            self.assertEqual(diverse(list(rows), {'a':10,'b':9,'c':2}, rows, 2), ['a','b'])

    def test_repeated_old_questions_do_not_hide_the_recalled_answer(self):
        query='Какая перчатка была выбрана для зала'
        rows={str(n):SimpleNamespace(memory_id=str(n),text=query) for n in range(5)}
        rows['answer']=SimpleNamespace(memory_id='answer',text='Для зала была выбрана синяя перчатка.')
        selected=diverse(list(rows), relevance(query, rows.values()), rows, 5)
        self.assertEqual(selected[:2], ['0','answer'])
        self.assertEqual(len(selected),5)
