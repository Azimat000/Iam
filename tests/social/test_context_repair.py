import json
from unittest.mock import patch

from iam.cycle.runner import input_id_for
from iam.domain import new_id
from iam.interfaces import Capability
from iam.social import SocialMemory
from iam.state import EventSpec
from iam.stubs import Reply, StubCapabilities, StubCreatorBridge, StubWorld, attend, say, rest
from iam.world import CAPABILITIES, PersistentWorld
from iam.world.cycle import LivingCycle
from world.helpers import LivingCase


class ContextCase(LivingCase):
    def setUp(self):
        super().setUp()
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        self.m = SocialMemory(self.c)
        self.world = PersistentWorld(self.c, external=StubWorld())
        self.caps = StubCapabilities([*CAPABILITIES, Capability('cap_Chat0001', 'Чат', 'Тест')])
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.c.resume_session()
        self.number = 0

    def incoming(self, text, *, channel='telegram', sender='123', read=True):
        self.number += 1
        iid = input_id_for(channel, str(self.number))
        event = self.c.record_event(EventSpec('input.arrived', f'rx:{channel}:{self.number}',
            {'input_id': iid, 'sender': sender, 'channel': channel, 'text': text})).event
        if not read:
            return None, event
        self.p.queue(Reply((attend('dialogue', iid),)))
        self.assertEqual(self.cycle.run(event).outcome, 'done')
        trigger = self.c.find_event(key='rd:'+iid)
        row = next(row for row in self.m.records() if trigger.event_id in row.sources)
        return row, trigger

    def exchange(self, question, answer, *, outcome='confirm', channel='telegram', sender='123', to='creator'):
        row, trigger = self.incoming(question, channel=channel, sender=sender)
        self.world.external.will('cap_Chat0001', outcome)
        self.p.queue(Reply((say('cap_Chat0001', to, answer),)))
        report = self.cycle.run(trigger)
        self.assertTrue(report.actions)
        return row, self.c.store.get_memory(report.actions[0].memory_id)

    def distract(self):
        hits = []
        for topic in ('сад', 'спорт', 'трамвай', 'дождь', 'ноты'):
            self.time.advance(1000)
            incoming, _ = self.exchange('Обсудим '+topic, 'Сегодня меня занимает '+topic)
            hits.append(incoming)
        return hits

    def request(self, text, records=None):
        row, trigger = self.incoming(text)
        self.p.queue(Reply((rest(),)))
        if records is None:
            self.cycle.run(trigger)
        else:
            with patch.object(self.m, 'recall', return_value=records):
                self.cycle.run(trigger)
        return self.p.calls[-1], row, trigger

    @staticmethod
    def packets(request, ref):
        return [json.loads(item['text']) for item in request['context']
                if item['ref'] == ref and item['text'].startswith('{')]


class SocialContextRepairTest(ContextCase):
    def test_neighbor_has_its_own_slot_after_five_retrieved_records(self):
        source, answer = self.exchange('Какую вещь возьмём в обсерваторию?', 'Бронзовый колокольчик.')
        hits = self.distract()
        initial = [source, *hits[:4]]
        request, _, _ = self.request('Что мы собирались взять?', initial)
        refs = {item['ref'] for item in request['context']}
        self.assertTrue({row.memory_id for row in initial} <= refs)
        self.assertIn(answer.memory_id, refs)

    def test_old_author_and_neighbor_survive_five_filled_recall_slots(self):
        source, answer = self.exchange('Назовём мастерскую «Орбита». Какую строку напишешь на вывеске?',
                                       'Здесь чинят маленькие механизмы больших надежд.')
        hits = self.distract()
        initial = [source, *hits[:4]]
        request, _, _ = self.request('Вернёмся к старой мастерской: кто предложил название и какую строку ты написала?', initial)
        personal = next(packet for packet in self.packets(request, source.memory_id) if 'text_window' in packet)
        self.assertEqual(personal.get('speaker'), 'interlocutor')
        self.assertEqual(personal.get('person'), 'creator')
        self.assertEqual(personal.get('direction'), 'incoming')
        refs = {item['ref'] for item in request['context']}
        self.assertTrue({row.memory_id for row in initial} <= refs)
        linked = next(packet for packet in self.packets(request, answer.memory_id)
                      if packet.get('recall_role') == 'neighbor')
        self.assertEqual(linked['speaker'], 'self')
        self.assertEqual(linked['outcome'], 'confirmed')
        self.assertNotIn('reply_to', linked)
        self.assertIn('маленькие механизмы', linked['text'])

    def test_neighbor_unknown_delivery_is_not_promoted_and_fade_does_not_bridge(self):
        source, answer = self.exchange('Что возьмём в дорогу?', 'ТОЛЬКО_ДОСТУПНОЕ_СООБЩЕНИЕ', outcome='unknown')
        hits = self.distract()
        request, _, _ = self.request('Вернёмся к дороге.', [source, *hits[:4]])
        packet = next(packet for packet in self.packets(request, answer.memory_id) if packet.get('recall_role') == 'neighbor')
        self.assertEqual(packet['outcome'], 'unknown')
        self.assertTrue(self.m.fade(answer.memory_id).applied)
        request, _, _ = self.request('Что было сказано?', [source, *hits[:4]])
        self.assertNotIn('ТОЛЬКО_ДОСТУПНОЕ_СООБЩЕНИЕ', json.dumps(request, ensure_ascii=False))
        linked = [json.loads(item['text']) for item in request['context'] if '"recall_role": "neighbor"' in item['text']]
        self.assertFalse(any(item.get('neighbor_of') == source.memory_id for item in linked))

    def test_unread_and_foreign_read_are_adjacency_barriers(self):
        for foreign_read in (False, True):
            source, _ = self.incoming('Первый разговор о маршруте')
            barrier, _ = self.incoming('НЕПРОЧИТАННЫЙ_ЧУЖОЙ_ТЕКСТ', channel='email', sender='other', read=foreign_read)
            if barrier is not None:
                self.assertTrue(self.m.fade(barrier.memory_id).applied)
            self.world.external.will('cap_Chat0001', 'confirm')
            self.p.queue(Reply((say('cap_Chat0001', 'creator', 'ОТДЕЛЬНЫЙ_ОТВЕТ_НЕ_СОСЕД'),)))
            report = self.cycle.run(self.due())
            answer = self.c.store.get_memory(report.actions[0].memory_id)
            hits = self.distract()
            request, _, _ = self.request('Вернёмся к первому разговору.', [source, *hits[:4]])
            self.assertNotIn('НЕПРОЧИТАННЫЙ_ЧУЖОЙ_ТЕКСТ', json.dumps(request, ensure_ascii=False))
            linked = [packet for packet in self.packets(request, answer.memory_id) if packet.get('recall_role') == 'neighbor']
            self.assertFalse(any(packet.get('neighbor_of') == source.memory_id for packet in linked))

    def test_neighbor_budget_is_fixed_even_for_large_text_limit_and_restart(self):
        source, answer = self.exchange('Что напишем на открытке?', 'Свет на бумаге. ' * 300 + 'НЕПОКАЗАННЫЙ_ХВОСТ')
        hits = self.distract()
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        self.world = PersistentWorld(self.c, external=StubWorld())
        self.m = SocialMemory(self.c, text_limit=6000)
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.c.resume_session()
        request, _, _ = self.request('Вернёмся к открытке.', [source, *hits[:4]])
        blocks = [item for item in request['context'] if '"recall_role": "neighbor"' in item['text']]
        self.assertTrue(blocks)
        self.assertLessEqual(sum(len(item['text']) for item in blocks), 2000)
        self.assertLessEqual(len(blocks), 2)
        self.assertNotIn('НЕПОКАЗАННЫЙ_ХВОСТ', json.dumps(request, ensure_ascii=False))
        packet = next(packet for packet in self.packets(request, answer.memory_id) if packet.get('recall_role') == 'neighbor')
        self.assertTrue(packet['text_window']['more'])
        window = packet['text_window']
        self.assertEqual(packet['text'], answer.text[window['start']:window['end']])
        self.assertTrue(self.m.revise(answer.memory_id, 'Пересмотренная запись', sources=answer.sources, reason='Поправка').applied)
        request, _, _ = self.request('Вернёмся к открытке.', [source, *hits[:4]])
        self.assertNotIn('НЕПОКАЗАННЫЙ_ХВОСТ', json.dumps(request, ensure_ascii=False))
        self.assertFalse(any(packet.get('recall_role') == 'neighbor' for packet in self.packets(request, answer.memory_id)))
        self.m.off = True
        request, _, _ = self.request_off('Непрочитанное новое сообщение')
        self.assertNotIn('Свет на бумаге', json.dumps(request, ensure_ascii=False))

    def request_off(self, text):
        _, arrival = self.incoming(text, read=False)
        self.p.queue(Reply((rest(),)))
        self.cycle.run(arrival)
        return self.p.calls[-1], None, arrival
