"""Исторические окна дополняют память, не вытесняя актуальные факты."""
import dataclasses
import json
from unittest.mock import patch

from iam.memory.search import historical_candidates
from perception.test_perception import PerceptionCase


class RecallRepairTest(PerceptionCase):
    def setUp(self):
        super().setUp()
        self.m = self.memory

    def remember(self, text, **changes):
        row = self.m.prepare(kind='episodic', origin='stated_by_other', text=text,
                             sources=(self.root(self.c),), claim_kind='fact_claim', independence_group='speaker')
        row = dataclasses.replace(row, **changes)
        result = self.m.write(row)
        self.assertTrue(result.applied, (changes, result))
        self.time.advance(100)
        return row

    def context_for(self, query, limit=5):
        iid, arrival = self.read(query, number=self.c.store.last_seq())
        trigger = self.c.find_event(key='rd:' + iid)
        found = self.m.recall(query, limit=limit, current=trigger.event_id)
        first = {'ref': trigger.event_id, 'origin': 'observation', 'layer': 'state', 'text': 'Прочитано сообщение.'}
        with patch.object(self.m, '_historical_context', return_value=[]):
            base = self.m.context(trigger, first, found)
        context = self.m.context(trigger, first, found)
        self.assertEqual(context[:len(base)], base)
        self.assertLessEqual(len(context) - len(base), 2)
        self.assertLessEqual(len(found), min(limit, self.m.working_limit))
        for item in context[len(base):]:
            self.assertLessEqual(len(item['text']), self.m.text_limit)
            self.assertEqual(json.loads(item['text'])['recall_role'], 'historical_episode')
        return found, context, trigger, arrival

    def test_early_episode_and_late_correction_survive_repeated_questions(self):
        for topic, detail in [('обсерватория', 'компас'), ('мастерская', 'колокольчик')]:
            first = self.remember(f'{topic}: я принёс {detail}, встреча 3 марта.')
            for index in range(6):
                self.remember(f'{topic}, {detail}: кто принёс? Вспомни встречу, вопрос номер {index}.')
            corrected = self.remember(f'{topic}: поправка, {detail} принёс не я; встреча 5 марта.')
            found, context, _, _ = self.context_for(f'{topic} {detail} встреча')
            self.assertIn(first.memory_id, {item['ref'] for item in context})
            self.assertIn(corrected.memory_id, {r.memory_id for r in found})
            self.assertEqual(len(found), 5)

    def test_unrelated_old_episode_cannot_displace_relevant_recent_fact(self):
        old = self.remember('Давняя прогулка вдоль озера.')
        current = self.remember('Обсерватория закрыта на ремонт до сентября.')
        for limit in (1, 2, 5):
            found, context, _, _ = self.context_for('обсерватория ремонт', limit)
            self.assertIn(current.memory_id, {r.memory_id for r in found})
            self.assertNotIn(old.memory_id, {item['ref'] for item in context})

    def test_hidden_revised_archived_sources_remain_excluded(self):
        hidden = []
        for change in ({'availability': 0}, {'detail': 0},
                       {'content': {'text': 'Обсерватория компас', 'superseded_by': 'mem_abcdefgh'}}):
            hidden.append(self.remember('Обсерватория компас', **change))
        archive = self.m.prepare(kind='episodic', origin='stated_by_other', text='Обсерватория компас архив',
                                 sources=(self.root(self.c),), claim_kind='fact_claim', independence_group='speaker')
        with self.c.store.transaction():
            self.c.store.add_memory(dataclasses.replace(archive, layer='archive'))
        visible = self.remember('Обсерватория компас доступны.')
        _, context, trigger, arrival = self.context_for('обсерватория компас')
        refs = {item['ref'] for item in context}
        self.assertIn(visible.memory_id, refs)
        self.assertFalse({row.memory_id for row in [*hidden, archive]} & refs)
        rows = self.m.records()
        self.assertEqual(self.m._historical_context(arrival, rows, []), [])
        current = next(row for row in rows if trigger.event_id in row.sources)
        self.assertNotIn(current.memory_id, {item['ref'] for item in self.m._historical_context(trigger, rows, [])})
        self.assertTrue(self.m.fade(current.memory_id).applied)
        self.assertEqual(self.m._historical_context(trigger, rows, []), [])
        self.m.off = True
        self.assertEqual(self.m.recall('обсерватория компас'), [])
        self.assertEqual(self.m._historical_context(trigger, rows, []), [])

    def test_exact_retrieval_does_not_use_historical_sampling(self):
        self.remember('Старинная обсерватория.')
        latest = self.remember('Новая обсерватория.')
        self.assertEqual(self.m.retrieve('новая')[0].memory_id, latest.memory_id)
        self.assertEqual(self.m.retrieve('неизвестное'), [])

    def test_historical_candidates_are_bounded_and_only_direct(self):
        rows = [self.remember(f'Обсерватория компас {i}') for i in range(12)]
        by_id = {r.memory_id:r for r in rows}
        self.assertEqual(historical_candidates('обсерватория компас', by_id, 0), [])
        found = historical_candidates('обсерватория компас', by_id, 5)
        self.assertEqual(found, [rows[2].memory_id, rows[3].memory_id])
        self.assertEqual(historical_candidates('озеро', by_id, 5), [])

    def correction_case(self, place, label):
        self.remember(f'{place}: ремонт обсуждали весной')
        self.remember(f'{place}: открытие обсуждали летом')
        for n in range(10, 14):
            self.remember(f'{place} ремонт открытие бюджет план — дата {n} октября?')
        correction = self.remember(f'{label}{place} закрыта, ремонт не завершён, открытие отменено')
        found, context, _, _ = self.context_for(f'{place} ремонт открытие бюджет план')
        self.assertIn(correction.memory_id, {r.memory_id for r in found})
        self.assertIn(correction.memory_id, {item['ref'] for item in context})

    def test_historical_context_keeps_late_correction_with_label(self):
        self.correction_case('Обсерватория', 'Поправка: ')

    def test_historical_context_keeps_late_correction_without_label(self):
        self.correction_case('Мастерская', '')

    def test_brief_authorship_fact_is_never_traded_for_long_old_text(self):
        for topic, label in [('рисунка', ''), ('романа', 'Уточнение: ')]:
            for season in ('весной', 'летом'):
                self.remember(f'Автор {topic} обсуждал выставку {season}. Музей и картина были интересной '
                              'темой нашего длинного разговора о композиции, материалах, цветах и впечатлениях.')
            for n in range(4):
                self.remember(f'Автор {topic} выставка музей картина — о ком мы тогда говорили? '
                              'Попробуй вспомнить наш разговор и подробности обсуждения экспонатов.')
            fact = self.remember(f'{label}Выставка, автор {topic} — Борис.')
            found, context, _, _ = self.context_for(f'Автор {topic} выставка музей картина')
            self.assertIn(fact.memory_id, {r.memory_id for r in found})
            self.assertIn(fact.memory_id, {item['ref'] for item in context})

    def test_extra_windows_do_not_duplicate_and_respect_tiny_limit(self):
        self.remember('Обсерватория ремонт старое основание ' * 20)
        _, context, trigger, _ = self.context_for('обсерватория ремонт')
        self.assertEqual(len(context), len({item['ref'] for item in context}))
        self.assertEqual(self.m._historical_context(trigger, [], context), [])
        self.m.text_limit = 1
        self.assertEqual(self.m._historical_context(trigger, self.m.records(), []), [])

    def test_extra_window_rechecks_safety_and_forgetting(self):
        old = self.remember('Обсерватория компас старый источник ' * 30)
        _, _, trigger, _ = self.context_for('обсерватория компас')
        rows = self.m.records()
        extra = self.m._historical_context(trigger, rows, [])
        self.assertIn(old.memory_id, {item['ref'] for item in extra})
        with patch.object(self.m, '_safe_item', return_value=False):
            self.assertEqual(self.m._historical_context(trigger, rows, []), [])
        self.assertTrue(self.m.fade(old.memory_id).applied)
        self.assertEqual(self.m._historical_context(trigger, rows, []), [])

    def test_extra_window_cap_does_not_grow_with_general_text_limit(self):
        self.m.text_limit = 6000
        self.remember('Обсерватория компас сведения о старом событии. ' * 100)
        _, _, trigger, _ = self.context_for('обсерватория компас')
        extra = self.m._historical_context(trigger, self.m.records(), [])
        self.assertTrue(extra)
        self.assertLessEqual(sum(len(item['text']) for item in extra), 4000)
        self.assertTrue(all(len(item['text']) <= 2000 for item in extra))
        original = self.m._window
        with patch.object(self.m, '_window', side_effect=lambda row, **kwargs:
                          None if kwargs.get('recall_role') else original(row, **kwargs)):
            self.assertEqual(self.m._historical_context(trigger, self.m.records(), []), [])
