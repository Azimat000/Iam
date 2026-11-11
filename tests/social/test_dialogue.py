"""Разговор помнит обе стороны, не возвращая забытое или непрочитанное."""
import json
from unittest.mock import patch

from iam.cycle import Cycle
from iam.clock import life_moment
from iam.domain import new_id
from iam.interfaces import Capability
from iam.social import SocialMemory
from iam.stubs import Reply, StubCapabilities, say, rest, attend
from perception.test_perception import PerceptionCase


class DialogueTest(PerceptionCase):
    def setUp(self):
        super().setUp()
        self.memory = SocialMemory(self.c)
        caps = StubCapabilities([Capability('cap_Chat0001', 'Чат', 'Тест')])
        self.cycle = Cycle(self.c, self.provider, self.memory, self.world, caps, self.creator)
        self.p = self.cycle.perception

    def answer(self, text, number=1, outcome='confirm'):
        iid, _ = self.read('Вопрос ' + str(number), number)
        self.world.will('cap_Chat0001', outcome)
        report = self.run_reply(self.c.find_event(key='rd:' + iid), say('cap_Chat0001', 'creator', text))
        self.assertTrue(report.actions, report)
        return self.c.store.get_memory(report.actions[0].memory_id)

    def turns(self):
        return [(item['ref'], json.loads(item['text'])) for item in self.memory.dialogue()]

    def test_real_cycle_has_both_sides_in_order_without_unread_text(self):
        answer = self.answer('Мой конкретный ответ')
        _, trigger = self.arrive('НЕПРОЧИТАННЫЙ_СЕКРЕТ', 2)
        self.run_reply(trigger, rest())
        context = self.provider.calls[-1]['context']
        turns = [(item['ref'], json.loads(item['text'])) for item in context
                 if item['layer'] == 'state' and '"dialogue_turn"' in item['text']]
        self.assertEqual([x[1]['direction'] for x in turns], ['incoming', 'outgoing'])
        self.assertLess(turns[0][1]['seq'], turns[1][1]['seq'])
        self.assertEqual(turns[1][0], answer.memory_id)
        self.assertNotEqual(turns[0][0], turns[1][0])
        self.assertEqual(turns[1][1]['text'], 'Мой конкретный ответ')
        self.assertEqual(turns[1][1]['outcome'], 'confirmed')
        self.assertNotIn('НЕПРОЧИТАННЫЙ_СЕКРЕТ', json.dumps(context, ensure_ascii=False))
        self.assertIn('Мой конкретный ответ', answer.text)
        self.assertEqual(answer.content['communication']['person'], 'creator')

    def test_unknown_and_failed_sending_keep_words_without_claiming_delivery(self):
        for n, behaviour in enumerate(('unknown', 'fail'), 1):
            answer = self.answer('Неизвестная доставка ' + str(n), n, behaviour)
            turn = next(t for ref, t in self.turns() if ref == answer.memory_id)
            self.assertEqual(turn['outcome'], {'unknown':'unknown', 'fail':'failed'}[behaviour])
            self.assertTrue(turn['content_available'])

    def test_fade_revision_and_off_do_not_restore_transport_copy(self):
        answer = self.answer('УДАЛЁННЫЕ_СЛОВА')
        derived = self.memory.prepare(kind='semantic', origin='own_hypothesis', text='Вывод', sources=(answer.memory_id,))
        self.assertTrue(self.memory.write(derived).applied)
        self.assertTrue(self.memory.fade(answer.memory_id).applied)
        self.assertNotIn('УДАЛЁННЫЕ_СЛОВА', json.dumps(self.memory.dialogue(), ensure_ascii=False))
        self.assertNotIn(derived.memory_id, [r.memory_id for r in self.memory.records()])
        answer = self.answer('ПРЕЖНЯЯ_ВЕРСИЯ', 2)
        incoming = next(r for r in self.memory.records() if r.origin == 'stated_by_other')
        self.assertTrue(self.memory.revise(answer.memory_id, 'Уточнённая запись', sources=(*answer.sources, incoming.memory_id), reason='Уточнение').applied)
        self.assertNotIn('ПРЕЖНЯЯ_ВЕРСИЯ', json.dumps(self.memory.dialogue(), ensure_ascii=False))
        self.memory.off = True
        self.assertEqual(self.memory.dialogue(), [])

    def test_legacy_outcome_does_not_hydrate_missing_words_from_actions(self):
        # Воспроизводим прежний писатель: собственные слова не попадали в личную память.
        def legacy(action, outcome, event):
            record = self.memory.prepare(kind='episodic', origin='observation', text='Telegram: sent',
                sources=(event.event_id,), content={'outcome':outcome.status,'experience':'live','action_id':action.action_id})
            return record, self.memory.write(record)
        with patch.object(self.memory, 'remember_outcome', side_effect=legacy):
            answer = self.answer('ТОЛЬКО_В_ТРАНСПОРТЕ')
        turn = next(t for ref, t in self.turns() if ref == answer.memory_id)
        self.assertFalse(turn['content_available'])
        self.assertEqual(turn['text'], '')
        self.assertNotIn('ТОЛЬКО_В_ТРАНСПОРТЕ', json.dumps(self.memory.dialogue(), ensure_ascii=False))

    def test_bounded_history_uses_explicit_excerpts_and_new_message_paging(self):
        text = ('слова "в кавычках"\n' * 200) + 'КОНЕЦ'
        answer = self.answer(text)
        turns = self.memory.dialogue()
        self.assertLessEqual(sum(len(t['text']) for t in turns), 2 * self.memory.text_limit)
        self.assertTrue(self.turns()[-1][1]['excerpt']['more'])
        self.assertTrue(self.c.schedule_moment(life_moment(new_id('mom'), self.c.life_time_ms, 'dialogue')).applied)
        trigger = self.c.fire_due_moments()[0].events[0]
        parts = []
        for _ in range(10):
            self.run_reply(trigger, attend('event', answer.memory_id))
            page = next(json.loads(x['text']) for x in self.provider.calls[-1]['context']
                        if x['ref'] == answer.memory_id and x['layer'] == 'personal')
            parts.append(page['text'])
            trigger = [e for e in self.c.events_after(0) if e.type == 'activity.completed'][-1]
            if not page['text_window']['more']:
                break
        self.assertEqual(''.join(parts), answer.text)
        for n in range(2, 6):
            self.answer('Ещё одна реплика ' + str(n), n)
        self.assertLessEqual(len(self.memory.dialogue()), 6)
        self.memory.text_limit = 1
        self.assertEqual(self.memory.dialogue(), [])

    def test_only_event_bound_episodes_enter_dialogue(self):
        self.answer('Обычный ответ')
        with patch.object(self.memory, '_source', return_value=None):
            self.assertEqual(self.memory.dialogue(), [])

    def test_late_reading_preserves_original_order_of_received_questions(self):
        earlier_id, earlier = self.arrive('БОЛЕЕ_РАННИЙ_ВОПРОС', 1)
        later_id, later = self.arrive('Уточнение', 2)
        self.run_reply(later, attend('dialogue', later_id))
        self.assertNotIn('БОЛЕЕ_РАННИЙ_ВОПРОС', json.dumps(self.provider.calls[-1], ensure_ascii=False))
        self.run_reply(earlier, attend('dialogue', earlier_id))
        turns = [turn for ref, turn in self.turns()]
        self.assertEqual([turn['text'] for turn in turns], ['Уточнение', 'БОЛЕЕ_РАННИЙ_ВОПРОС'])
        self.assertLess(turns[0]['seq'], turns[1]['seq'])
        self.assertEqual([turn['received_seq'] for turn in turns], [later.seq, earlier.seq])

    def test_current_read_is_distinct_from_old_reply_and_new_unread_notice(self):
        answer = self.answer('Предыдущая собственная реплика')
        self.time.advance(1000)
        iid, trigger = self.arrive('НОВЫЙ_СКРЫТЫЙ_ВОПРОС', 2)
        self.run_reply(trigger, attend('dialogue', iid))
        context = self.provider.calls[-1]['context']
        self.assertNotIn('НОВЫЙ_СКРЫТЫЙ_ВОПРОС', json.dumps(context, ensure_ascii=False))
        self.assertIn('прежние прочитанные реплики', context[0]['text'])
        turns = [json.loads(x['text']) for x in context if '"dialogue_turn"' in x['text']]
        self.assertFalse(any(t['current_read'] for t in turns))
        own = next(t for t in turns if t['direction'] == 'outgoing')
        self.assertTrue(own['latest_in_direction'])
        self.assertEqual(own['age_life_ms'], 1000)
        read = self.c.find_event(key='rd:' + iid)
        self.run_reply(read, rest())
        context = self.provider.calls[-1]['context']
        turns = [(x['ref'],json.loads(x['text'])) for x in context if '"dialogue_turn"' in x['text']]
        current = [(ref,t) for ref,t in turns if t['current_read']]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0][1]['text'], 'НОВЫЙ_СКРЫТЫЙ_ВОПРОС')
        self.assertTrue(current[0][1]['latest_in_direction'])
        self.assertNotEqual(current[0][0], answer.memory_id)
        self.assertEqual(sum(t['latest_in_direction'] for ref,t in turns), 2)
        self.assertTrue(self.memory.fade(current[0][0]).applied)
        self.assertFalse(any(json.loads(t['text'])['current_read'] for t in self.memory.dialogue(read)))
        self.memory.off = True
        self.assertEqual(self.memory.dialogue(read), [])

    def test_overview_opens_current_memory_when_not_in_normal_retrieval(self):
        from iam.memory.window import next_window
        answer = self.answer('ДЛИННЫЙ_ОТВЕТ ' * 250)
        overview = self.memory.dialogue()
        own = next(item for item in overview if item['ref'] == answer.memory_id)
        self.assertTrue(json.loads(own['text'])['excerpt']['more'])
        self.assertEqual(self.p._page_offset(answer.memory_id, [own]), (answer, 0))
        # Даже если обзор стоит раньше личного окна, продолжаем окно, а не зацикливаем начало.
        personal = {'ref': answer.memory_id, 'layer': 'personal', 'text': json.dumps({
            'version': answer.version, 'text_window': {'end': 100}})}
        self.assertEqual(next_window(answer, [own, personal]), 100)
        stale = dict(own, text=json.dumps({**json.loads(own['text']), 'version':0}))
        self.assertIsNone(next_window(answer, [stale]))
        unrelated = dict(own, text=json.dumps({'version':answer.version, 'kind':'unrelated_state'}))
        self.assertIsNone(next_window(answer, [unrelated]))
        # Полный обычный цикл: retrieval ничего не выбрал, обзор всё же позволяет открыть память.
        self.c.schedule_moment(life_moment(new_id('mom'), self.c.life_time_ms, 'dialogue'))
        trigger = self.c.fire_due_moments()[0].events[0]
        with patch.object(self.memory, 'retrieve', return_value=[]):
            result = self.run_reply(trigger, attend('event', answer.memory_id))
        self.assertEqual(result.outcome, 'done')
        page_event = [e for e in self.c.events_after(0) if e.type == 'activity.completed'][-1]
        self.assertEqual(page_event.payload['offset'], 0)
        self.run_reply(page_event, rest())
        page = next(json.loads(x['text']) for x in self.provider.calls[-1]['context']
                    if x['ref'] == answer.memory_id and x['layer'] == 'personal')
        self.assertEqual(page['text_window']['start'], 0)
        self.assertTrue(self.memory.fade(answer.memory_id).applied)
        self.assertIsNone(self.p._page_offset(answer.memory_id, [own]))

