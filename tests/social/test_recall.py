import json
from unittest.mock import patch

from iam.state import verify, EventSpec
from iam.cycle import Cycle
from iam.interfaces import Capability
from iam.social import SocialMemory
from iam.stubs import say, rest, attend, StubCapabilities
from perception.test_perception import PerceptionCase
from social.test_dialogue import DialogueTest


class RecallTest(PerceptionCase):
    turns = DialogueTest.turns

    def setUp(self):
        super().setUp()
        self.memory = SocialMemory(self.c)
        caps = StubCapabilities([Capability('cap_Chat0001', 'Чат', 'Тест')])
        self.cycle = Cycle(self.c, self.provider, self.memory, self.world, caps, self.creator)
        self.p = self.cycle.perception

    def exchange(self, incoming, outgoing, number):
        iid, arrival = self.read(incoming, number)
        self.world.will('cap_Chat0001', 'confirm')
        report = self.run_reply(self.c.find_event(key='rd:' + iid), say('cap_Chat0001', 'creator', outgoing))
        self.assertTrue(report.actions, report)
        return self.c.store.get_memory(report.actions[0].memory_id)

    def history(self):
        own = self.exchange('Откроем кафе для путешественников. Какую вещь поставишь у входа?',
                            'Поставлю медный компас, который показывает домой.', 1)
        for n, topic in enumerate(('музыка', 'соседи', 'работа', 'погода', 'прогулка'), 2):
            self.exchange('Теперь тема: '+topic, 'Мне интересна тема: '+topic, n)
        return own

    def test_real_cycle_recalls_old_answer_through_neighbor_without_enlarging_context(self):
        own = self.history()
        self.assertNotIn(own.memory_id, [ref for ref, _ in self.turns()])
        iid, _ = self.read('Вернёмся к кафе для путешественников: какую вещь ты выбрала?', 10)
        trigger = self.c.find_event(key='rd:' + iid)
        self.run_reply(trigger, rest())
        request = self.provider.calls[-1]
        records = [item for item in request['context'] if item['layer'] in ('personal','interpretation')]
        found = next(item for item in records if item['ref'] == own.memory_id)
        self.assertIn('медный компас', found['text'])
        self.assertEqual(json.loads(found['text'])['observation_scope'], 'outgoing_words_only')
        event = [e for e in self.c.events_after(0) if e.type == 'memory.retrieved'][-1]
        self.assertIn('кафе', event.payload['query'])
        self.assertLessEqual(len(event.payload['memory_ids']), 5)
        self.assertLessEqual(len(records), self.memory.working_limit)
        self.assertEqual(verify(self.c.store), [])

    def test_unread_words_never_become_query_or_change_rank(self):
        self.history()
        _, arrival = self.arrive('НЕПРОЧИТАННОЕ кафе компас', 10)
        self.run_reply(arrival, rest())
        query = [e.payload['query'] for e in self.c.events_after(0) if e.type == 'memory.retrieved'][-1]
        self.assertEqual(query, '')
        self.assertNotIn('НЕПРОЧИТАННОЕ', json.dumps(self.provider.calls[-1], ensure_ascii=False))

    def test_faded_read_cue_and_off_never_restore_query_from_event(self):
        iid, _ = self.read('СЕКРЕТ_ЗАПРОСА компас', 1)
        trigger = self.c.find_event(key='rd:' + iid)
        record = next(r for r in self.memory.records() if trigger.event_id in r.sources)
        self.assertTrue(self.memory.fade(record.memory_id).applied)
        self.assertEqual(self.memory.retrieval_query(trigger, 'old'), '')
        self.run_reply(trigger, rest())
        self.assertNotIn('СЕКРЕТ_ЗАПРОСА', json.dumps(self.provider.calls[-1], ensure_ascii=False))
        self.memory.off = True
        self.assertEqual(self.memory.retrieval_query(trigger, 'old'), '')
        self.assertEqual(self.memory.recall('компас'), [])

    def test_neighbor_does_not_restore_faded_or_revised_words(self):
        own = self.history()
        self.assertTrue(self.memory.fade(own.memory_id).applied)
        self.assertNotIn(own.memory_id, [r.memory_id for r in self.memory.recall('кафе путешественников')])
        current = self.exchange('Кинотеатр во дворе', 'СТАРЫЙ_ОТВЕТ о кино', 11)
        self.assertTrue(self.memory.revise(current.memory_id, 'Уточнение', sources=current.sources, reason='Правка').applied)
        self.assertNotIn('СТАРЫЙ_ОТВЕТ', str([r.text for r in self.memory.recall('Кинотеатр')]))

    def test_no_transport_search_and_limits_remain_enforced(self):
        own = self.history()
        with patch.object(self.c.store, 'search_memory', side_effect=AssertionError('archive/FTS not needed')):
            self.assertTrue(self.memory.recall('кафе'))
        self.assertEqual(len(self.memory.recall('кафе', limit=1)), 1)
        self.assertEqual(self.memory.recall('кафе', limit=0), [])
        self.assertEqual(self.memory.recall('несуществующиймаркер'), [])
        with self.assertRaises(ValueError): self.memory.recall('кафе', limit=-1)

    def test_query_uses_only_same_visible_window_of_current_version(self):
        iid, _ = self.read(('Обсерватория '*500)+'НЕПОКАЗАННЫЙ_ХВОСТ', 1)
        trigger = self.c.find_event(key='rd:' + iid)
        self.run_reply(trigger, rest())
        current = next(item for item in self.provider.calls[-1]['context'] if item['layer']=='personal')
        query = [e.payload['query'] for e in self.c.events_after(0) if e.type=='memory.retrieved'][-1]
        self.assertEqual(query, json.loads(current['text'])['text'])
        self.assertNotIn('НЕПОКАЗАННЫЙ_ХВОСТ', query)
        record = self.c.store.get_memory(current['ref'])
        self.assertTrue(self.memory.revise(record.memory_id, 'Изменённый запрос', sources=record.sources, reason='Правка').applied)
        self.assertEqual(self.memory.retrieval_query(trigger), 'Изменённый запрос')
        self.memory.text_limit = 1
        self.assertEqual(self.memory.retrieval_query(trigger), '')

    def test_neighbor_does_not_cross_interlocutors(self):
        arrival = self.c.record_event(EventSpec('input.arrived', 'rx:email:1',
            {'input_id':'inp_OtherInbox01', 'sender':'other', 'channel':'email',
             'text':'Обсерватория другого собеседника'}, event_id='evt_OtherInbox01')).event
        self.run_reply(arrival, attend('dialogue', 'inp_OtherInbox01'))
        read = self.c.find_event(key='rd:inp_OtherInbox01')
        self.world.will('cap_Chat0001','confirm')
        report = self.run_reply(read, say('cap_Chat0001','creator','Отдельное сообщение создателю'))
        own = report.actions[0].memory_id
        self.assertNotIn(own, [r.memory_id for r in self.memory.recall('обсерватория')])

    def test_recalled_authorship_and_unknown_delivery_remain_explicit(self):
        iid, _ = self.read('Предлагаю, чтобы маяк каждую ночь рассказывал разные истории.', 1)
        read = self.c.find_event(key='rd:'+iid)
        source = next(r for r in self.memory.records() if read.event_id in r.sources)
        self.world.will('cap_Chat0001','unknown')
        report = self.run_reply(read,say('cap_Chat0001','creator','Мне нравится ненадёжный рассказчик.'))
        own = report.actions[0].memory_id
        for n in range(2,7):
            self.exchange('Другая тема '+str(n),'Обсуждаем другое '+str(n),n)
        iid, _ = self.read('Кто предложил, чтобы маяк рассказывал разные истории?',10)
        self.run_reply(self.c.find_event(key='rd:'+iid),rest())
        context = self.provider.calls[-1]['context']
        theirs = next(x for x in context if x['ref']==source.memory_id and x['layer']=='personal')
        self.assertEqual(theirs['origin'], 'stated_by_other')
        mine = next(x for x in context if x['ref']==own and x['layer']=='personal')
        self.assertEqual(json.loads(mine['text'])['outcome'], 'unknown')
