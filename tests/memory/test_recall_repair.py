"""Ранние эпизоды и поздние поправки делят ограниченный бюджет воспоминаний."""
import dataclasses
import unittest

from iam.memory import PersistentMemory
from iam.memory.search import historical
from state.helpers import StateCase


class RecallRepairTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)
        self.m = PersistentMemory(self.c)

    def remember(self, text, **changes):
        row = self.m.prepare(kind='episodic', origin='stated_by_other', text=text,
                             sources=(self.root(self.c),), claim_kind='fact_claim',
                             independence_group='speaker')
        row = dataclasses.replace(row, **changes)
        result = self.m.write(row)
        self.assertTrue(result.applied, (changes, result))
        self.time.advance(100)
        return row

    def test_early_episode_and_late_correction_survive_repeated_questions(self):
        for topic, detail in [('обсерватория', 'компас'), ('мастерская', 'колокольчик')]:
            first = self.remember(f'{topic}: я принёс {detail}, встреча 3 марта.')
            for index in range(6):
                self.remember(f'{topic}, {detail}: кто принёс? Вспомни встречу, вопрос номер {index}.')
            corrected = self.remember(f'{topic}: поправка, {detail} принёс не я; встреча 5 марта.')
            query = f'{topic} {detail} встреча'
            found = self.m.recall(query, limit=5)
            self.assertIn(first.memory_id, {r.memory_id for r in found})
            self.assertIn(corrected.memory_id, {r.memory_id for r in found})
            self.assertEqual(len(found), 5)
            self.assertEqual(next(r.text for r in found if r.memory_id == corrected.memory_id), corrected.text)

    def test_unrelated_old_episode_cannot_displace_relevant_recent_fact(self):
        old = self.remember('Давняя прогулка вдоль озера.')
        current = self.remember('Обсерватория закрыта на ремонт до сентября.')
        for limit in (1, 2, 5):
            found = self.m.recall('обсерватория ремонт', limit=limit)
            self.assertEqual([r.memory_id for r in found], [current.memory_id])
            self.assertNotIn(old.memory_id, {r.memory_id for r in found})

    def test_hidden_revised_archived_and_current_sources_remain_excluded(self):
        for change in ({'availability': 0}, {'detail': 0},
                       {'content': {'text': 'Обсерватория компас', 'superseded_by': 'mem_abcdefgh'}}):
            self.remember('Обсерватория компас', **change)
        archive = self.m.prepare(kind='episodic', origin='stated_by_other', text='Обсерватория компас архив',
                                 sources=(self.root(self.c),), claim_kind='fact_claim', independence_group='speaker')
        with self.c.store.transaction():
            self.c.store.add_memory(dataclasses.replace(archive, layer='archive'))
        visible = self.remember('Обсерватория компас доступны.')
        self.assertEqual([r.memory_id for r in self.m.recall('обсерватория компас')], [visible.memory_id])
        self.assertEqual(self.m.recall('обсерватория компас', current=visible.sources[0]), [])
        self.m.off = True
        self.assertEqual(self.m.recall('обсерватория компас'), [])

    def test_exact_retrieval_does_not_use_historical_sampling(self):
        self.remember('Старинная обсерватория.')
        latest = self.remember('Новая обсерватория.')
        self.assertEqual(self.m.retrieve('новая')[0].memory_id, latest.memory_id)
        self.assertEqual(self.m.retrieve('неизвестное'), [])

    def test_sampling_preserves_small_budgets_and_limits_candidate_pool(self):
        rows = [self.remember(f'Обсерватория компас {i}') for i in range(12)]
        by_id = {r.memory_id: r for r in rows}
        ranked = [r.memory_id for r in reversed(rows)]
        for limit in (1, 2):
            self.assertEqual(historical(ranked, ranked[:limit], by_id, limit), ranked[:limit])
        found = historical(ranked, ranked[:5], by_id, 5)
        self.assertEqual(len(found), len(set(found)))
        self.assertEqual(len(found), 5)
        self.assertNotIn(rows[0].memory_id, found)  # за пределами первых 2N совпадений
        # Разные числа — отдельные свидетельства, ради старого совпадения они не удаляются.
        self.assertEqual(found, ranked[:5])

    def test_historical_sampling_keeps_late_correction_with_or_without_label(self):
        for place in ('Обсерватория', 'Мастерская'):
            for label in ('Поправка: ', ''):
                records = [self.remember(f'{place}: ремонт обсуждали весной'),
                           self.remember(f'{place}: открытие обсуждали летом')]
                records.extend(self.remember(f'{place} ремонт открытие бюджет план — дата {n} октября?')
                               for n in range(10, 14))
                correction = self.remember(f'{label}{place} закрыта, ремонт не завершён, открытие отменено')
                records.append(correction)
                # Изолируем четыре случаи: каждый остаётся настоящими доступными записями.
                original = self.m.records
                self.m.records = lambda: records
                try:
                    selected = self.m.recall(f'{place} ремонт открытие бюджет план', limit=5)
                finally:
                    self.m.records = original
                self.assertIn(correction.memory_id, {r.memory_id for r in selected})
                self.assertEqual(len(selected), 5)

    def test_sole_author_cannot_be_replaced(self):
        rows = [self.remember('Обсерватория ремонт обсуждали весной')]
        rows += [self.remember('Обсерватория ремонт обсуждали позже') for _ in range(3)]
        unique = self.remember('Обсерватория ремонт завершён', independence_group='another_speaker')
        rows.append(unique)
        by_id = {r.memory_id:r for r in rows}
        selected = [r.memory_id for r in rows[1:]]
        found = historical([r.memory_id for r in rows], selected, by_id, 4)
        self.assertIn(unique.memory_id, found)
        self.assertEqual(len(found), 4)

    def test_distinct_delivery_outcome_is_not_replaced_by_old_text(self):
        rows = [self.remember('Порт: корабль прибыл утром')]
        rows += [self.remember('Порт корабль пришёл вечером') for _ in range(3)]
        rows += [self.remember('Порт корабль прибыл')]
        # Проверка самого отбора: разные исходы и отправители не сливаются.
        for field, value in (('outcome', 'unknown'), ('communication', {'person': 'another'})):
            altered = dataclasses.replace(rows[-1], content={**rows[-1].content, field: value})
            by_id = {r.memory_id:r for r in [*rows[:-1], altered]}
            selected = [r.memory_id for r in rows[1:]]
            found = historical([r.memory_id for r in rows], selected, by_id, 4)
            self.assertIn(altered.memory_id, found)

    def test_old_candidate_without_new_information_does_not_force_replacement(self):
        old = self.remember('Обсерватория ремонт')
        rows = [self.remember('Обсерватория ремонт открытие') for _ in range(3)]
        rows.append(self.remember('Обсерватория ремонт открытие вечером'))
        by_id = {r.memory_id:r for r in [old, *rows]}
        selected = [r.memory_id for r in rows]
        self.assertEqual(historical([old.memory_id, *selected], selected, by_id, 4), selected)
