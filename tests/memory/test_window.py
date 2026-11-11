"""Полнота прочитанного и явное продолжение при ограниченном рабочем объёме."""
import json
from types import SimpleNamespace

from iam.memory.window import next_window, text_window
from iam.stubs import Reply, attend, rest
from iam.state import verify
from perception.test_perception import PerceptionCase


class WindowTest(PerceptionCase):
    def context_page(self, ref):
        return next(json.loads(x['text']) for x in self.provider.calls[-1]['context']
                    if x['ref'] == ref and x['layer'] == 'personal')

    def test_477_character_message_tail_changes_the_actual_request(self):
        for n, time in enumerate(('10:00', '18:00'), 1):
            text = 'Рассказываю о прошедшем дне. ' * 15 + ' Важное уточнение: встреча завтра в ' + time + '.'
            iid, _ = self.read(text, n)
            read = self.c.find_event(key='rd:' + iid)
            self.provider.queue(Reply((rest(),)))
            self.cycle.run(read)
            row = next(r for r in self.memory.records() if read.event_id in r.sources)
            page = self.context_page(row.memory_id)
            self.assertEqual(page['text'], text)
            self.assertFalse(page['text_window']['more'])
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), self.memory.text_limit)

    def test_long_message_is_read_in_full_by_voluntary_pages_without_new_input(self):
        text = ('строка "цитата"\\\n\t' * 500) + ' КОНЕЦ: встреча в 18:00.'
        iid, _ = self.read(text)
        trigger = self.c.find_event(key='rd:' + iid)
        row = self.memory.records()[0]
        parts = []
        for _ in range(30):
            self.provider.queue(Reply((attend('event', row.memory_id),)))
            report = self.cycle.run(trigger)
            self.assertEqual(report.outcome, 'done', report)
            page = self.context_page(row.memory_id)
            parts.append(page['text'])
            self.assertEqual(page['text_window']['start'], sum(map(len, parts[:-1])))
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), self.memory.text_limit)
            trigger = [e for e in self.c.events_after(0) if e.type == 'activity.completed'][-1]
            if not page['text_window']['more']:
                break
        else:
            self.fail('Чтение не дошло до конца')
        self.assertEqual(''.join(parts), text)
        self.assertEqual(sum(e.type == 'input.read' for e in self.c.events_after(0)), 1)
        self.assertEqual(self.c.store.get_memory(row.memory_id).text, text)
        # После последней страницы добровольное перечитывание возвращается к началу.
        self.provider.queue(Reply((rest(),)))
        self.cycle.run(trigger)
        self.assertEqual(self.context_page(row.memory_id)['text_window']['start'], 0)
        self.assertEqual(verify(self.c.store), [])

    def test_hidden_and_forgotten_memory_cannot_be_opened_or_recovered_by_page_event(self):
        iid, trigger = self.arrive('Непрочитанное')
        report = self.run_reply(trigger, attend('event', 'mem_Unknown000'))
        self.assertEqual(report.outcome, 'rejected')
        iid, _ = self.read('Страница ' * 600, 2)
        row = self.memory.records()[0]
        read = self.c.find_event(key='rd:' + iid)
        self.provider.queue(Reply((attend('event', row.memory_id),)))
        self.cycle.run(read)
        page_event = [e for e in self.c.events_after(0) if e.type == 'activity.completed'][-1]
        self.assertTrue(self.memory.fade(row.memory_id).applied)
        self.provider.queue(Reply((attend('event', row.memory_id),)))
        self.assertEqual(self.cycle.run(page_event).outcome, 'rejected')
        self.assertNotIn('Страница', json.dumps(self.provider.calls[-1], ensure_ascii=False))
        self.memory.off = True
        self.assertIsNone(self.p._page_offset(row.memory_id, []))

    def test_current_version_and_malformed_context_do_not_open_a_page(self):
        self.read('текст')
        row = self.memory.records()[0]
        item = {'ref': row.memory_id, 'layer': row.layer, 'text': ''}
        for data in ('bad json', '[]', '{"version": 99}', '{"version": 1}',
                     '{"version": 1,"text_window":{"end":false}}'):
            self.assertIsNone(next_window(row, [dict(item, text=data)]))
        self.assertIsNone(next_window(row, [dict(item, ref='mem_Unknown00')]))
        self.assertIsNone(next_window(row, [dict(item, layer='state')]))
        self.assertIsNone(self.p._page_offset(row.memory_id, [item]))
        self.assertIsNone(text_window({}, 'text', 1))
        self.assertEqual(json.loads(text_window({}, '', 500))['text'], '')
        self.assertEqual(json.loads(text_window({}, '"\\\tя'*100, 500))['text_window']['more'], True)
        # Старое событие страницы не открывает исправленную версию воспоминания.
        fake = SimpleNamespace(type='activity.completed', event_id='evt_OldPage00',
                               payload={'memory_id':row.memory_id,'memory_version':0,'offset':1})
        first = {'ref': fake.event_id, 'layer':'state','origin':'observation','text':'Страница'}
        self.assertEqual(self.memory.context(fake, first, []), [first])

    def test_page_event_is_not_independent_evidence_and_derivation_fades(self):
        iid, _ = self.read('Длинное воспоминание ' * 300)
        original = self.memory.records()[0]
        self.provider.queue(Reply((attend('event', original.memory_id),)))
        self.cycle.run(self.c.find_event(key='rd:' + iid))
        page = [e for e in self.c.events_after(0) if e.type == 'activity.completed'][-1]
        illicit = self.memory.prepare(kind='semantic', origin='own_hypothesis', text='Вывод из страницы', sources=(page.event_id,))
        self.assertFalse(self.memory.write(illicit).applied)
        derivative = self.memory.prepare(kind='semantic', origin='own_hypothesis', text='Вывод из оригинала', sources=(original.memory_id,))
        self.assertTrue(self.memory.write(derivative).applied)
        self.assertTrue(self.memory.fade(original.memory_id).applied)
        self.assertNotIn(derivative.memory_id, [r.memory_id for r in self.memory.records()])
        self.assertIsNone(self.memory._source(page.event_id))

    def test_page_rechecks_availability_after_acceptance(self):
        from unittest.mock import patch
        iid, _ = self.read('Длинное воспоминание ' * 300)
        original = self.memory.records()[0]
        self.assertIsNone(self.p._page_offset('mem_Unknown00', []))
        finish = self.p.finish
        def disable(pr, accepted):
            self.memory.off = True
            return finish(pr, accepted)
        self.provider.queue(Reply((attend('event', original.memory_id),)))
        with patch.object(self.p, 'finish', side_effect=disable), self.assertRaisesRegex(ValueError, 'больше недоступна'):
            self.cycle.run(self.c.find_event(key='rd:' + iid))
        self.assertFalse(any(e.type == 'activity.completed' for e in self.c.events_after(0)))

    def test_real_codex_adapter_opens_next_memory_page(self):
        from iam.providers.codex.fake import reply_text
        from providers.test_codex import provider
        iid, _ = self.read('Часть сообщения ' * 400 + 'КОНЕЦ')
        row = self.memory.records()[0]
        wire = {'proposals':[], 'explanation':'Продолжу читать', 'attention':{
            'rationale':'В сообщении осталась непрочитанная часть','cites':[], 'focus':'event','ref':row.memory_id}}
        model, runner = provider(reply_text(json.dumps(wire)))
        self.cycle.provider = model
        report = self.cycle.run(self.c.find_event(key='rd:' + iid))
        self.assertEqual(report.outcome, 'done', report)
        page = [e for e in self.c.events_after(0) if e.type == 'activity.completed'][-1]
        self.assertGreater(page.payload['offset'], 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertIsNone(next_window(row, [{'ref':row.memory_id,'layer':row.layer,
            'text':'{"version":1,"text_window":"invalid"}'}]))

    def test_revision_invalidates_old_page_and_its_derivative_without_archive_read(self):
        iid, _ = self.read('СТАРЫЕ_СЛОВА ' * 500)
        original = self.memory.records()[0]
        self.provider.queue(Reply((attend('event', original.memory_id),)))
        self.cycle.run(self.c.find_event(key='rd:' + iid))
        page = [e for e in self.c.events_after(0) if e.type == 'activity.completed'][-1]
        derivative = self.memory.prepare(kind='semantic', origin='own_hypothesis', text='Вывод до исправления', sources=(original.memory_id,))
        self.assertTrue(self.memory.write(derivative).applied)
        correction_id, _ = self.read('Поправка: встреча перенесена', 2)
        correction_event = self.c.find_event(key='rd:' + correction_id)
        correction = next(r for r in self.memory.records() if correction_event.event_id in r.sources)
        self.assertTrue(self.memory.revise(original.memory_id, 'Встреча перенесена', sources=(correction.memory_id,), reason='Поправка автора').applied)
        self.assertNotIn(derivative.memory_id, [r.memory_id for r in self.memory.records()])
        self.provider.queue(Reply((rest(),)))
        self.cycle.run(page)
        self.assertNotIn('СТАРЫЕ_СЛОВА', json.dumps(self.provider.calls[-1], ensure_ascii=False))
        self.assertIsNone(self.memory._source(page.event_id))
