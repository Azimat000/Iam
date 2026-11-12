import json
from unittest.mock import patch

from iam.stubs import Reply, say, rest
from social.test_context_repair import ContextCase
from iam.world.conversation import recollection_sources
from iam.world.discussion import discussion_work


class SourceProjectRepairTest(ContextCase):
    def discuss(self, work, *, to='creator', outcome='confirm', cites=None):
        _, trigger = self.incoming('Что думаешь об этой работе?')
        self.world.external.will('cap_Chat0001', outcome)
        self.p.queue(Reply((say('cap_Chat0001', to, 'Меня занимает оставленный фонарь.',
                               cites=cites if cites is not None else [work.memory_id]),)))
        with patch.object(self.m, 'recall', return_value=[work]):
            report = self.cycle.run(trigger)
        self.assertTrue(report.actions)
        return self.c.store.get_memory(report.actions[0].memory_id)

    @staticmethod
    def catalog(request):
        return next(json.loads(i['text']) for i in request['context'] if i['text'].startswith('{"library"'))

    def test_discussed_project_survives_new_version_other_project_and_restart(self):
        old = self.write('СТАРЫЙ_МАЯК', project='lighthouse')
        spoken = self.discuss(old)
        self.time.advance(1000)
        latest = self.write('Новый маяк: смотритель оставляет красный фонарь у лестницы.', project='lighthouse')
        self.time.advance(1000)
        other = self.write('В саду закрывают калитку.', project='garden')
        self.c.pause_session('operator');self.c = self.reopen(self.c)
        from iam.social import SocialMemory
        from iam.world import PersistentWorld
        from iam.world.cycle import LivingCycle
        from iam.stubs import StubWorld, StubCreatorBridge
        self.m = SocialMemory(self.c);self.world = PersistentWorld(self.c, external=StubWorld())
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.c.resume_session()
        request, _, _ = self.request('Какая деталь осталась в последнем варианте?', [other])
        results = self.catalog(request)['world_results']
        self.assertEqual(results[0]['memory_id'], latest.memory_id)
        self.assertEqual(results[0]['discussion_ref'], spoken.memory_id)
        self.assertIn('красный фонарь', results[0]['text'])
        self.assertTrue(results[0]['current_work'])
        self.assertIn(other.memory_id, {r['memory_id'] for r in results})
        block = next(p for p in self.packets(request, latest.memory_id) if p.get('discussion_ref'))
        self.assertEqual(block['project_id'], 'lighthouse')
        self.assertLessEqual(len(json.dumps(block,ensure_ascii=False)) + len(json.dumps(results[0],ensure_ascii=False)),2000)

    def test_visible_discussed_work_is_not_duplicated_and_preserves_full_catalog(self):
        work = self.write('Длинная работа. '*180 + 'КОНЕЦ_ТЕКСТА', project='lighthouse')
        spoken = self.discuss(work)
        request, _, _ = self.request('Как закончилась работа?', [work])
        results = self.catalog(request)['world_results']
        self.assertEqual(len([r for r in results if r['memory_id']==work.memory_id]),1)
        self.assertTrue(results[0]['text'].endswith('КОНЕЦ_ТЕКСТА'))
        self.assertEqual(results[0]['discussion_ref'],spoken.memory_id)

    def test_hidden_latest_does_not_promote_old_or_another_project(self):
        old = self.write('Старый маяк', project='lighthouse');self.discuss(old)
        self.time.advance(1000);latest = self.write('СКРЫТЫЙ_ФИНАЛ', project='lighthouse')
        other = self.write('Калитка',project='garden')
        self.assertTrue(self.m.fade(latest.memory_id).applied)
        request, _, _ = self.request('Что в последнем варианте?', [old,other])
        self.assertNotIn('СКРЫТЫЙ_ФИНАЛ',json.dumps(request,ensure_ascii=False))
        unavailable = [json.loads(i['text']) for i in request['context'] if '"kind": "discussion_project"' in i['text']]
        self.assertEqual(unavailable[0]['project_id'],'lighthouse')
        self.assertFalse(unavailable[0]['available_current_result'])
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
        self.assertFalse(next(r for r in self.catalog(request)['world_results'] if r['memory_id']==old.memory_id)['current_work'])

    def test_unavailable_or_revised_cited_source_cannot_link_project(self):
        for mode in ('fade','revise'):
            work = self.write('ЗАПИСЬ_ОСНОВАНИЯ_'+mode, project=mode)
            self.discuss(work)
            self.time.advance(1000);self.write('Новая версия '+mode, project=mode)
            other=self.write('Другая работа',project='other')
            if mode=='fade':self.assertTrue(self.m.fade(work.memory_id).applied)
            else:self.assertTrue(self.m.revise(work.memory_id,'Исправлено',sources=work.sources,reason='Поправка').applied)
            request, _, _ = self.request('Продолжим?', [other])
            self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
            self.assertNotIn('ЗАПИСЬ_ОСНОВАНИЯ_'+mode,json.dumps(request,ensure_ascii=False))

    def test_foreign_unknown_and_unread_do_not_link(self):
        work=self.write('ТЕКСТ_МАЯКА',project='lighthouse')
        self.discuss(work,to='another')
        other=self.write('Калитка',project='garden')
        request, _, _=self.request('Что дальше?', [other])
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
        self.discuss(work,outcome='unknown')
        request, _, _=self.request('Что дальше?', [other])
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
        _,event=self.incoming('НЕПРОЧИТАННОЕ_СЛОВО',read=False)
        self.p.queue(Reply((rest(),)));self.cycle.run(event)
        self.assertNotIn('НЕПРОЧИТАННОЕ_СЛОВО',json.dumps(self.p.calls[-1],ensure_ascii=False))
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(self.p.calls[-1])['world_results']))
        self.m.off=True
        self.p.queue(Reply((rest(),)));self.cycle.run(self.due())
        self.assertFalse(self.catalog(self.p.calls[-1])['world_results'])

    def test_direct_source_is_in_final_frame_despite_old_refusal(self):
        source,_=self.exchange('Предлагаю называть корабль «Аврора».','Хорошо.')
        self.exchange('Кто предложил название?','Не помню, кто это предложил.')
        request,current,_=self.request('А кто предложил название корабля?', [source])
        frame=json.loads(request['context'][-1]['text'])
        sources=frame['recollection_sources']
        primary=next(s for s in sources if s['ref']==source.memory_id)
        self.assertEqual(primary['speaker'],'interlocutor')
        self.assertEqual(primary['quote'],source.text)
        self.assertNotIn(current.memory_id,{s['ref'] for s in sources})
        self.assertIn('прежнее знание',frame['guidance'])
        self.assertIn('Не помню',json.dumps(request,ensure_ascii=False))
        self.assertTrue(self.m.fade(source.memory_id).applied)
        request,_,_=self.request('А кто предложил название корабля?')
        self.assertNotIn(source.memory_id,{s['ref'] for s in json.loads(request['context'][-1]['text'])['recollection_sources']})

    def test_source_digest_bounds_and_invalid_packets(self):
        context=[]
        for n in range(8):
            data={'statement_scope':'utterance_only','speaker':'interlocutor','person':'creator','seq':n,
                  'outcome':'read','text':'Показанный текст '*100,'text_window':{'start':10,'end':1610,'total':2000,'more':True}}
            context.append({'ref':'mem_'+str(n),'layer':'personal','text':json.dumps(data,ensure_ascii=False)})
        context += [context[0], {'ref':'bad','layer':'personal','text':'bad'}, {'ref':'list','layer':'personal','text':'[]'},
                    {'ref':'other','layer':'personal','text':'{}'}]
        result=recollection_sources(context,'mem_7')
        self.assertLessEqual(len(result),4);self.assertLessEqual(len(json.dumps(result,ensure_ascii=False)),2000)
        self.assertEqual(len({s['ref'] for s in result}),len(result))
        self.assertTrue(result[0]['excerpt']['more']);self.assertEqual(result[0]['excerpt']['start'],10)
        self.assertLessEqual(len(result[0]['quote']),400)
        self.assertEqual(discussion_work(self.m,self.world.state,[{'ref':'bad','layer':'state','text':'bad'}]),([],None))

    def test_rejected_duplicate_id_cannot_rewrite_delivered_message_provenance(self):
        first=self.write('Маяк',project='lighthouse');spoken=self.discuss(first)
        pid=self.c.find_event(event_id=spoken.sources[0]).payload['proposal_id']
        other=self.write('Сад',project='garden')
        duplicate=say('cap_Chat0001','creator','Не должен менять старую доставку',cites=[other.memory_id])
        duplicate['proposal_id']=pid
        report=self.run_reply(duplicate)
        self.assertEqual(report.outcome,'rejected')
        request,_,_=self.request('Каков последний вариант обсуждавшейся работы?',[other])
        result=self.catalog(request)['world_results'][0]
        self.assertEqual(result['project_id'],'lighthouse')
        self.assertEqual(result['discussion_ref'],spoken.memory_id)

    def test_reference_chain_requires_actual_delivery_and_matching_request(self):
        import copy
        from dataclasses import replace
        from iam.world.discussion import delivered_proposal
        work=self.write('Маяк',project='lighthouse');spoken=self.discuss(work)
        event=self.c.find_event(event_id=spoken.sources[0])
        self.assertIsNotNone(delivered_proposal(self.m,event))
        find=self.c.find_event
        chain=[];parent=event
        for _ in range(4):
            parent=find(event_id=parent.causes[0]);chain.append(parent)
        for parent in chain:
            with patch.object(self.c,'find_event',side_effect=lambda *,event_id=None,key=None,missing=parent.event_id:
                              None if event_id==missing else find(event_id=event_id,key=key)):
                self.assertIsNone(delivered_proposal(self.m,event))
        for key in ('proposal_id','cycle_id'):
            payload={**event.payload,key:'wrong'}
            self.assertIsNone(delivered_proposal(self.m,replace(event,payload=payload)))
        accepted=chain[1]
        wrong=replace(accepted,payload={**accepted.payload,'request_id':'wrong'})
        with patch.object(self.c,'find_event',side_effect=lambda *,event_id=None,key=None:
                          wrong if event_id==wrong.event_id else find(event_id=event_id,key=key)):
            self.assertIsNone(delivered_proposal(self.m,event))
        received=chain[2];payload=copy.deepcopy(received.payload);payload['response']['proposals']=[]
        wrong=replace(received,payload=payload)
        with patch.object(self.c,'find_event',side_effect=lambda *,event_id=None,key=None:
                          wrong if event_id==wrong.event_id else find(event_id=event_id,key=key)):
            self.assertIsNone(delivered_proposal(self.m,event))
        _,trigger=self.incoming('Вернёмся к работе')
        context=self.m.dialogue(trigger)
        with patch.object(self.c,'find_event',side_effect=lambda *,event_id=None,key=None:
                          None if event_id==event.event_id else find(event_id=event_id,key=key)):
            self.assertEqual(discussion_work(self.m,self.world.state,context),([],None))

    def test_unconfirmed_cited_action_blocked_project_and_short_digest(self):
        work=self.write('Маяк',project='lighthouse');self.discuss(work)
        latest=self.write('Поздний маяк',project='lighthouse')
        _,trigger=self.incoming('Продолжим?');context=self.m.dialogue(trigger)
        state=self.world.state
        state['actions'][work.content['action_id']]['status']='failed'
        self.assertEqual(discussion_work(self.m,state,context),([],None))
        self.assertTrue(self.m.fade(latest.memory_id).applied)
        with patch.object(self.m,'_safe_item',return_value=False):
            self.assertEqual(discussion_work(self.m,self.world.state,context),([],None))
        short=[]
        for n in range(6):
            short.append({'ref':str(n),'layer':'personal','text':json.dumps({
                'statement_scope':'utterance_only','speaker':'interlocutor','person':'creator','seq':n,
                'outcome':'read','text':'Да','text_window':{'start':0,'end':2,'total':2,'more':False}})})
        self.assertEqual(len(recollection_sources(short,None)),4)
