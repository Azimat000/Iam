"""Current conversation is explicit without forcing a choice or revealing unread/forgotten text."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from iam.interfaces import Capability
from iam.providers.codex import build_prompt
from iam.social import SocialMemory
from iam.state import verify
from iam.stubs import Reply, StubCapabilities, attend, rest, say
from iam.world import PersistentWorld, CAPABILITIES
from iam.world.cycle import LivingCycle
from iam.world.conversation import interaction_context
from perception.test_perception import PerceptionCase


class InteractionTest(PerceptionCase):
    def setUp(self):
        super().setUp()
        self.memory = SocialMemory(self.c)
        world = PersistentWorld(self.c, external=self.world)
        world.initialize()
        caps = StubCapabilities((*CAPABILITIES, Capability('cap_Chat0001','Чат','Тест')))
        self.cycle = LivingCycle(self.c, self.provider, self.memory, world, caps, self.creator)
        self.p = self.cycle.perception

    def frame(self):
        context = self.provider.calls[-1]['context']
        frame = json.loads(context[-1]['text'])
        self.assertEqual(frame['kind'], 'current_interaction')
        # The actual provider receives the same available data, not an alternative DB path.
        self.assertEqual(json.loads(build_prompt(self.provider.calls[-1]))['context'][-1]['text'],context[-1]['text'])
        return frame

    def answer(self, text, number=1):
        iid, _ = self.read('Вопрос '+str(number),number)
        self.world.will('cap_Chat0001','confirm')
        report = self.run_reply(self.c.find_event(key='rd:'+iid),say('cap_Chat0001','creator',text))
        self.assertTrue(report.actions,report)
        return self.c.store.get_memory(report.actions[0].memory_id)

    def test_current_turn_and_unread_targets_are_distinct_from_previous_words(self):
        iid, _ = self.read('Прежняя тема',1)
        self.world.will('cap_Chat0001','confirm')
        self.run_reply(self.c.find_event(key='rd:'+iid),say('cap_Chat0001','creator','Мой прежний ответ'))
        old = self.frame()['read_now']['ref']
        own = next(r for r in self.memory.records() if 'communication' in r.content)
        iid, arrival = self.arrive('Смена темы: ты хочешь поговорить сейчас?',2)
        state = copy.deepcopy(self.c.state)
        self.run_reply(arrival,attend('dialogue',iid))
        frame = self.frame()
        self.assertEqual(frame['unread_targets'],[iid])
        self.assertIsNone(frame['read_now'])
        self.assertEqual((frame['latest_read_ref'],frame['last_outgoing']['ref']),(old,own.memory_id))
        self.assertEqual(frame['last_outgoing']['outcome'],'confirmed')
        self.assertNotIn('Смена темы:',json.dumps(self.provider.calls[-1],ensure_ascii=False))
        self.run_reply(self.c.find_event(key='rd:'+iid),rest())
        frame = self.frame()
        self.assertEqual(frame['read_now']['text'],'Смена темы: ты хочешь поговорить сейчас?')
        self.assertEqual(frame['read_now']['received_seq'],arrival.seq)
        self.assertNotEqual(frame['read_now']['ref'],old)
        self.assertEqual(frame['last_outgoing']['ref'],own.memory_id)
        self.assertEqual(frame['unread_targets'],[])
        self.assertEqual(self.c.state['traits'],state['traits'])
        self.assertEqual(verify(self.c.store),[])

    def test_delivery_failures_remain_distinct_from_words_received_by_interlocutor(self):
        for number, behaviour in enumerate(('confirm','fail','unknown'),1):
            iid,_=self.read('Реплика '+str(number),number*2)
            self.world.will('cap_Chat0001',behaviour)
            self.run_reply(self.c.find_event(key='rd:'+iid),say('cap_Chat0001','creator','Попытка '+str(number)))
            _,arrival=self.arrive('Следующее сообщение',number*2+1)
            self.run_reply(arrival,rest())
            outgoing=self.frame()['last_outgoing']
            self.assertEqual(outgoing['outcome'],{'confirm':'confirmed','fail':'failed','unknown':'unknown'}[behaviour])
            self.assertTrue(outgoing['content_available'])
            self.assertEqual(outgoing['speaker'],'self')
            self.assertEqual(outgoing['text'],'Попытка '+str(number))
            record=self.c.store.get_memory(outgoing['ref'])
            self.assertEqual(record.content['communication']['text'],'Попытка '+str(number))

    def test_interlocutor_closing_is_not_presented_as_own_previous_words(self):
        own = self.answer('Выбираю соседа Геннадия из-за шороха листьев.')
        iid,arrival = self.read('Спасибо за разговор. До встречи!',2)
        self.run_reply(self.c.find_event(key='rd:'+iid),rest())
        frame = self.frame()
        self.assertEqual(frame['read_now']['speaker'],'interlocutor')
        self.assertEqual(frame['read_now']['text'],'Спасибо за разговор. До встречи!')
        self.assertEqual(frame['read_now']['received_seq'],arrival.seq)
        outgoing = frame['last_outgoing']
        self.assertEqual((outgoing['speaker'],outgoing['ref'],outgoing['version']),('self',own.memory_id,1))
        self.assertEqual(outgoing['text'],'Выбираю соседа Геннадия из-за шороха листьев.')
        self.assertEqual(outgoing['outcome'],'confirmed')
        self.assertLess(outgoing['seq'],arrival.seq)
        # Declining to answer is still valid and does not manufacture a delivered reply.
        self.assertEqual(len([e for e in self.c.events_after(0) if e.type=='message.out_sent']),1)
        self.assertEqual(verify(self.c.store),[])

    def test_last_outgoing_keeps_the_same_bounded_excerpt(self):
        self.answer(('Длинный собственный ответ '*200)+'СКРЫТЫЙ_КОНЕЦ')
        iid,_ = self.read('Новый вопрос',2)
        self.run_reply(self.c.find_event(key='rd:'+iid),rest())
        frame = self.frame()['last_outgoing']
        turn = next(json.loads(x['text']) for x in self.provider.calls[-1]['context']
                    if x['ref']==frame['ref'] and '"dialogue_turn"' in x['text'])
        self.assertEqual((frame['text'],frame['excerpt']),(turn['text'],turn['excerpt']))
        self.assertTrue(frame['excerpt']['more'])
        self.assertLess(len(frame['text']),800)
        self.assertNotIn('СКРЫТЫЙ_КОНЕЦ',json.dumps(frame,ensure_ascii=False))

    def test_own_words_are_not_resurrected_after_fade_revision_or_memory_off(self):
        for n,mode in enumerate(('fade','revise','off'),1):
            text='НЕ_ВОЗВРАЩАТЬ_'+mode
            own=self.answer(text,n*2)
            if mode=='fade':self.assertTrue(self.memory.fade(own.memory_id).applied)
            elif mode=='revise':
                self.assertTrue(self.memory.revise(own.memory_id,'Пересмотрено',sources=own.sources,reason='Уточнение').applied)
            else:self.memory.off=True
            _,arrival=self.arrive('Ещё непрочитано',n*2+1)
            self.run_reply(arrival,rest())
            self.assertIsNone(self.frame()['last_outgoing'])
            self.assertNotIn(text,json.dumps(self.provider.calls[-1],ensure_ascii=False))

    def test_legacy_delivery_without_words_stays_unknown_in_summary(self):
        def legacy(action,outcome,event):
            record=self.memory.prepare(kind='episodic',origin='observation',text='Telegram: sent',
                sources=(event.event_id,),content={'outcome':outcome.status,'experience':'live','action_id':action.action_id})
            return record,self.memory.write(record)
        with patch.object(self.memory,'remember_outcome',side_effect=legacy):
            self.answer('ТОЛЬКО_В_АРХИВЕ_ДОСТАВКИ')
        _,arrival=self.arrive('Новый вопрос',2)
        self.run_reply(arrival,rest())
        outgoing=self.frame()['last_outgoing']
        self.assertEqual(outgoing['outcome'],'confirmed')
        self.assertFalse(outgoing['content_available'])
        self.assertIsNone(outgoing['text'])
        self.assertIsNone(outgoing['excerpt'])
        self.assertNotIn('ТОЛЬКО_В_АРХИВЕ_ДОСТАВКИ',json.dumps(self.provider.calls[-1],ensure_ascii=False))

    def test_ignoring_is_allowed_and_pending_notice_remains_available(self):
        iid, event = self.arrive('UNREAD_SECRET',1)
        self.run_reply(event,rest())
        self.assertEqual(self.frame()['unread_targets'],[iid])
        self.assertFalse(any(e.type=='input.read' for e in self.c.events_after(0)))
        self.assertNotIn('UNREAD_SECRET',json.dumps(self.provider.calls[-1]))
        # A later unrelated opportunity still describes the pending input, without opening it.
        trigger = SimpleNamespace(type='activity.completed',event_id=event.event_id)
        context = self.provider.calls[-1]['context'][:-1]
        frame = json.loads(interaction_context(trigger,context)[0]['text'])
        self.assertEqual(frame['unread_targets'],[iid])
        self.assertIsNone(frame['read_now'])

    def test_read_frame_does_not_restore_faded_revised_or_disabled_memory(self):
        for number, mode in enumerate(('fade','revise','off'),1):
            text='NO_REHYDRATION_'+mode
            iid,_=self.read(text,number)
            read=self.c.find_event(key='rd:'+iid)
            row=next(r for r in self.memory.records() if r.sources==(read.event_id,))
            if mode=='fade':self.assertTrue(self.memory.fade(row.memory_id).applied)
            elif mode=='revise':
                self.assertTrue(self.memory.revise(row.memory_id,'Исправленная запись',sources=row.sources,reason='Уточнение').applied)
            else:self.memory.off=True
            self.run_reply(read,rest())
            self.assertIsNone(self.frame()['read_now'])
            self.assertNotIn(text,json.dumps(self.provider.calls[-1],ensure_ascii=False))

    def test_long_text_stays_an_excerpt_and_late_read_keeps_receiving_order(self):
        first,earlier=self.arrive('РАНЬШЕ '+('длинная реплика '*200),1)
        second,later=self.arrive('Позднее уточнение',2)
        self.run_reply(later,attend('dialogue',second))
        self.run_reply(earlier,attend('dialogue',first))
        self.run_reply(self.c.find_event(key='rd:'+first),rest())
        frame=self.frame()
        self.assertEqual(frame['read_now']['received_seq'],earlier.seq)
        self.assertTrue(frame['read_now']['excerpt']['more'])
        self.assertLess(len(frame['read_now']['text']),800)
        self.assertLess(len(json.dumps(frame,ensure_ascii=False)),3000)


class FrameBoundaryTest(unittest.TestCase):
    def test_only_visible_metadata_is_used_and_context_is_not_modified(self):
        def item(ref,text,layer='state'):
            return {'ref':ref,'layer':layer,'origin':'observation','text':text}
        context=[item('evt_Example01','plain instructions'),item('evt_Example02','[]'),
                 item('mem_Private01','{"text":"PRIVATE_RECORD"}','personal'),
                 item('evt_Example03','{"kind":"dialogue_turn"}')]
        trigger=SimpleNamespace(type='time.moment_due',event_id='evt_Trigger01',payload={'text':'HIDDEN_TRIGGER'})
        before=copy.deepcopy(context)
        self.assertEqual(interaction_context(trigger,context),[])
        for n in range(7):context.append(item('inp_Example0'+str(n),'{}','notice'))
        frame=json.loads(interaction_context(trigger,context)[0]['text'])
        self.assertEqual(len(frame['unread_targets']),5)
        self.assertTrue(frame['more_unread_targets'])
        self.assertEqual(context[:4],before)
        self.assertNotIn('HIDDEN_TRIGGER',json.dumps(frame))
        self.assertNotIn('PRIVATE_RECORD',json.dumps(frame))
