"""Поздний разговор возвращает продолжение, не только совпавшую фразу."""
import json
from unittest.mock import patch

from iam.memory.window import next_window
from iam.providers.codex.presentation import present_context, restore_context
from iam.social.episodes import episode_context
from iam.social import SocialMemory
from iam.world import PersistentWorld
from iam.world.cycle import LivingCycle
from iam.stubs import StubWorld, StubCreatorBridge
from iam.state import verify
from iam.world.speech import speech_context
from social.test_context_repair import ContextCase


class EpisodeTest(ContextCase):
    @staticmethod
    def episodes(request):
        return [(item, json.loads(item['text'])) for item in request['context']
                if '"kind": "conversation_episode"' in item['text']]

    def history(self):
        original, answer = self.exchange('Пусть книжка будет зелёной, «Лишний четверг».', 'Название мне нравится.')
        correction, reply = self.exchange('Передумал: пусть она будет синей и называется «После дождя».',
                                          'Второе мне нравится больше, но первое тоже нравилось.')
        self.distract()
        return original, answer, correction, reply

    def test_late_anaphoric_correction_and_own_position_survive_restart(self):
        original, answer, correction, reply = self.history()
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        self.m = SocialMemory(self.c)
        self.world = PersistentWorld(self.c, external=StubWorld())
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.c.resume_session()
        row, trigger = self.incoming('Какой цвет и название у нашей книжки?')
        # Реальный recall, не вручную заданные найденные записи.
        records = self.m.recall(row.text, current=trigger.event_id)
        result = episode_context(self.m, trigger, records, [])
        data = {item['ref']: json.loads(item['text']) for item in result}
        for source in (original, answer, correction, reply):
            self.assertIn(source.memory_id, data)
            self.assertFalse(data[source.memory_id]['excerpt']['more'])
        self.assertEqual(data[correction.memory_id]['text'], correction.text)
        self.assertEqual(data[reply.memory_id]['text'], reply.content['communication']['text'])
        self.assertEqual(data[reply.memory_id]['speaker'], 'self')
        self.assertEqual(data[correction.memory_id]['speaker'], 'interlocutor')
        self.assertEqual(data[reply.memory_id]['outcome'], 'confirmed')
        self.assertEqual(verify(self.c.store), [])

    def test_projection_and_paging_keep_episode_words_and_canonical_offsets_separate(self):
        original, answer, correction, reply = self.history()
        request, _, _ = self.request('Вернёмся к книжке.', [original])
        episodes = [item for item, _ in self.episodes(request)]
        self.assertEqual(len(episodes), 6)
        projected = speech_context(episodes)
        self.assertEqual(projected, episodes)
        view = present_context(projected)
        self.assertEqual(len(view["conversation"]["recalled_utterances"]), 6)
        self.assertEqual(view["conversation"]["recent_turns"], [])
        self.assertEqual(restore_context(view), projected)
        own = next(item for item in episodes if item['ref'] == reply.memory_id)
        self.assertEqual(next_window(reply, [own]), 0)
        self.assertEqual(json.loads(own['text'])['text'], reply.content['communication']['text'])
        self.assertNotEqual(json.loads(own['text'])['text'], reply.text)

    def test_unread_foreign_faded_and_revised_gaps_are_not_bridged(self):
        for barrier in ('unread', 'foreign', 'faded', 'revised'):
            with self.subTest(barrier=barrier):
                seed, answer = self.exchange('Книжка о космосе '+barrier, 'Подходит.')
                if barrier == 'unread':
                    self.incoming('СКРЫТЫЙ_ТЕКСТ', read=False)
                elif barrier == 'foreign':
                    self.exchange('ЧУЖАЯ_РЕПЛИКА', 'Чужой ответ', channel='email', sender='other', to='other')
                else:
                    gap, _ = self.exchange('СКРЫТАЯ_ПРОМЕЖУТОЧНАЯ', 'Промежуточный ответ')
                    if barrier == 'faded':
                        self.assertTrue(self.m.fade(gap.memory_id).applied)
                    else:
                        self.assertTrue(self.m.revise(gap.memory_id, 'Пересмотрено', sources=gap.sources,
                                                      reason='Правка').applied)
                follow, _ = self.exchange('НЕ_ПРОДОЛЖЕНИЕ', 'Отдельный ответ')
                self.distract()
                request, _, _ = self.request('Книжка о космосе '+barrier, [seed])
                episodes = self.episodes(request)
                refs = {item['ref'] for item, _ in episodes}
                self.assertIn(seed.memory_id, refs)
                self.assertNotIn(follow.memory_id, refs)
                self.assertNotIn('СКРЫТЫЙ_ТЕКСТ', json.dumps(episodes, ensure_ascii=False))

    def test_faded_or_revised_answer_does_not_restore_words_or_connect_later(self):
        for mode in ('fade', 'revise'):
            original, answer, correction, reply = self.history()
            if mode == 'fade':
                self.assertTrue(self.m.fade(answer.memory_id).applied)
            else:
                self.assertTrue(self.m.revise(answer.memory_id, 'Иное', sources=answer.sources, reason='Правка').applied)
            request, _, trigger = self.request('Книжка?', [original])
            # Проверяем только связь от этого основания, без самостоятельных совпадений.
            packets = episode_context(self.m, trigger, [original], [])
            refs = {item['ref'] for item in packets}
            self.assertEqual(refs, {original.memory_id})

    def test_consecutive_read_correction_and_unknown_outcome_remain_distinct(self):
        original, _ = self.incoming('Книжка сначала красная.')
        correction, answer = self.exchange('Передумал, пусть будет жёлтой.', 'Мне ближе красная.', outcome='unknown')
        self.distract()
        request, _, _ = self.request('Вернёмся к книжке.', [original])
        packets = {item['ref']: data for item, data in self.episodes(request)}
        self.assertIn(correction.memory_id, packets)
        self.assertEqual(packets[answer.memory_id]['outcome'], 'unknown')
        self.assertNotIn('reply_to', packets[answer.memory_id])

    def test_budget_off_empty_small_limit_and_safe_filter(self):
        original, _ = self.exchange('Книжка '+('слово '*1000), 'Текст '*1000+'НЕПОКАЗАННЫЙ_ХВОСТ')
        self.exchange('Поправка '+('слово '*1000), 'Другой текст '*1000)
        self.distract()
        request, _, trigger = self.request('Книжка?', [original])
        episodes = self.episodes(request)
        self.assertLessEqual(sum(len(item['text']) for item, _ in episodes), 10000)
        self.assertLessEqual(len(episodes), 12)
        self.assertTrue(any(data['excerpt']['more'] for _, data in episodes))
        self.assertNotIn('НЕПОКАЗАННЫЙ_ХВОСТ', json.dumps(episodes, ensure_ascii=False))
        self.m.text_limit = 1
        self.assertEqual(episode_context(self.m, trigger, [original], []), [])
        self.m.text_limit = 8000
        with patch.object(self.m, '_safe_item', return_value=False):
            self.assertEqual(episode_context(self.m, trigger, [original], []), [])
        self.assertEqual(episode_context(self.m, trigger, [], []), [])
        self.assertTrue(episode_context(self.m, trigger, [original], [{'ref': 'unrelated'}]))
        with patch.object(self.m, 'retrieval_query', return_value='НЕСУЩЕСТВУЮЩЕЕ'):
            self.assertEqual(episode_context(self.m, trigger, [original], []), [])
        self.m.off = True
        self.assertEqual(episode_context(self.m, trigger, [original], []), [])

    def test_long_weak_old_episode_cannot_displace_stronger_correction(self):
        old, _ = self.exchange('Название радиостанции. '+('Старые слова. '*300), 'Старая передача. '*300)
        self.exchange('Продолжим эфир. '+('Старые слова. '*300), 'Старая история. '*300)
        self.exchange('Последняя передача. '+('Старые слова. '*300), 'Старая пауза. '*300)
        best, _ = self.exchange('Книжка цвет название. '+('Новые слова. '*300), 'Новый ответ. '*300)
        correction, reply = self.exchange('Она теперь охряная, «Вторая платформа».', 'Второе название мне ближе.')
        self.distract()
        _, trigger = self.incoming('Книжка цвет название?')
        result = episode_context(self.m, trigger, [old, best], [])
        refs = {item['ref'] for item in result}
        self.assertIn(correction.memory_id, refs)
        self.assertIn(reply.memory_id, refs)
        self.assertLessEqual(sum(len(item['text']) for item in result), 10000)
        self.assertEqual([json.loads(item['text'])['seq'] for item in result],
                         sorted(json.loads(item['text'])['seq'] for item in result))
