import json
from unittest.mock import patch
from social.test_context_repair import ContextCase
from world.test_source_project_repair import SourceProjectRepairTest
from iam.world.discussion import discussion_work
from iam.world.subject import subject_candidates, project_source


class CurrentSubjectRepairTest(ContextCase):
    catalog = staticmethod(SourceProjectRepairTest.catalog)
    discuss = SourceProjectRepairTest.discuss

    def uncertainty(self, request):
        return [json.loads(i['text']) for i in request['context'] if '"kind": "discussion_project"' in i['text']]

    def test_explicit_subject_overrides_wrong_outgoing_and_other_activity(self):
        first = self.write('На маяке смотритель оставил фонарь.', project='lighthouse')
        wrong = self.write('У калитки сада стояла лейка.', project='garden')
        self.discuss(wrong)
        self.time.advance(1000)
        latest = self.write('На маяке смотритель оставил синий фонарь.', project='lighthouse')
        other = self.write('У аквариума плавала рыбка.', project='aquarium')
        request, current, _ = self.request('Какая деталь последней сцены на маяке тебе нравится?', [wrong,other])
        result = self.catalog(request)['world_results'][0]
        self.assertEqual(result['project_id'], 'lighthouse')
        self.assertEqual(result['memory_id'], latest.memory_id)
        self.assertEqual(result['discussion_ref'], current.memory_id)
        self.assertIn('синий фонарь', result['text'])
        self.assertNotEqual(first.memory_id, result['memory_id'])
        self.discuss(other)
        request, current, _ = self.request('Вернусь к маяку: что в последнем варианте?', [other])
        result = self.catalog(request)['world_results'][0]
        self.assertEqual(result['project_id'],'lighthouse')
        self.assertEqual(result['discussion_ref'],current.memory_id)
        _,trigger=self.incoming('Вернусь к маяку')
        extra,result=discussion_work(self.m,self.world.state,self.m.dialogue(trigger))
        self.assertTrue(extra)
        self.assertLessEqual(len(extra[0]['text'])+len(json.dumps(result,ensure_ascii=False)),2000)

    def test_primary_history_beats_wrong_outgoing_with_no_lexical_current_subject(self):
        right=self.write('В обсерватории звезда отражалась в стекле.',project='observatory')
        wrong=self.write('На острове спал краб.',project='island')
        source,_=self.incoming('Обсудим обсерваторию.')
        self.discuss(wrong)
        request,_,_=self.request('Каков последний вариант?', [wrong,source])
        result=self.catalog(request)['world_results'][0]
        self.assertEqual(result['project_id'],'observatory')
        self.assertEqual(result['discussion_ref'],source.memory_id)
        self.assertEqual(result['memory_id'],right.memory_id)

    def test_ambiguous_current_and_return_do_not_promote_wrong_reply(self):
        one=self.write('Ракета летела.',project='rocket')
        two=self.write('Подводная лодка плыла.',project='submarine')
        self.discuss(one)
        request,_,_=self.request('Сравним ракету и подводную лодку.', [two])
        block=self.uncertainty(request)[0]
        self.assertTrue(block['ambiguous'])
        self.assertEqual({i['project_id'] for i in block['candidates']},{'rocket','submarine'})
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
        request,_,_=self.request('А какая деталь нравится?', [two])
        self.assertTrue(self.uncertainty(request)[0]['ambiguous'])
        with patch.object(self.m,'_safe_item',return_value=False):
            _,trigger=self.incoming('Ракета и подводная лодка')
            self.assertEqual(discussion_work(self.m,self.world.state,self.m.dialogue(trigger)),([],None))

    def test_hidden_latest_reports_unavailable_and_never_uses_old_as_current(self):
        old=self.write('Скрипка лежала у окна.',project='violin')
        latest=self.write('Скрипка: СЕКРЕТНЫЙ_НОВЫЙ_ФИНАЛ',project='violin')
        wrong=self.write('Листва в лесу.',project='forest');self.discuss(wrong)
        self.assertTrue(self.m.fade(latest.memory_id).applied)
        request,_,_=self.request('Что сейчас в сцене со скрипкой?', [old,wrong])
        block=self.uncertainty(request)[0]
        self.assertEqual(block['project_id'],'violin');self.assertFalse(block['available_current_result'])
        self.assertNotIn('СЕКРЕТНЫЙ_НОВЫЙ_ФИНАЛ',json.dumps(request,ensure_ascii=False))
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
        self.assertFalse(next(r for r in self.catalog(request)['world_results'] if r['memory_id']==old.memory_id)['current_work'])

    def test_foreign_hidden_revised_and_unread_primary_cannot_select(self):
        right=self.write('Журавль стоит у реки.',project='crane')
        wrong=self.write('В подвале погас свет.',project='cellar')
        foreign,_=self.incoming('Журавль',channel='email',sender='stranger')
        request,_,_=self.request('Каков последний вариант?', [wrong,foreign])
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
        source,_=self.incoming('Журавль')
        self.assertTrue(self.m.revise(source.memory_id,'Изменено',sources=source.sources,reason='Поправка').applied)
        request,_,_=self.request('Каков последний вариант?', [wrong])
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
        self.assertTrue(self.m.fade(right.memory_id).applied)
        request,_,_=self.request('А журавль?', [wrong])
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
        _,trigger=self.incoming('Журавль',read=False)
        self.assertEqual(discussion_work(self.m,self.world.state,self.m.dialogue(trigger)),([],None))
        self.m.off=True
        self.assertEqual(discussion_work(self.m,self.world.state,[]),([],None))

    def test_only_exact_visible_window_and_confirmed_action_are_evidence(self):
        from dataclasses import replace
        work=self.write('Телескоп стоит у окна.',project='telescope')
        row,trigger=self.incoming('Телескоп')
        context=self.m.dialogue(trigger);turns=[(i,json.loads(i['text'])) for i in context]
        current=next(t for _,t in turns if t.get('current_read'))
        rows={r.memory_id:r for r in self.m.records()}
        self.assertEqual(subject_candidates(self.m,self.world.state,context,turns,current,rows),{'telescope':row.memory_id})
        for change in ({'text':'ПОДМЕНА'}, {'excerpt':{}}, {'excerpt':[]}, {'excerpt':{'start':-1,'end':3}}, {'direction':'outgoing'}, {'person':'stranger'}):
            fake={**current,**change};items=[{'ref':row.memory_id,'text':json.dumps(fake)}]
            self.assertIsNone(subject_candidates(self.m,self.world.state,items,[(items[0],fake)],fake,rows))
        for text in ('broken','[]'):
            self.assertIsNone(subject_candidates(self.m,self.world.state,[{'ref':'bad','text':text}],[],current,rows))
        self.assertIsNone(subject_candidates(self.m,self.world.state,context,turns,current,{}))
        state=self.world.state
        state['actions'][work.content['action_id']]['status']='failed'
        self.assertIsNone(project_source(self.m,state,work))
        self.assertIsNone(project_source(self.m,self.world.state,replace(work,version=2)))
        with patch.object(self.c,'find_event',return_value=None):
            self.assertIsNone(project_source(self.m,self.world.state,work))
        self.assertIsNone(subject_candidates(self.m,self.world.state,[],[],current,rows))

    def test_many_ambiguous_candidates_remain_bounded(self):
        terms=('абрикос','баклажан','виноград','гранат','дыня','ежевика','земляника','кабачок','лимон')
        for n,term in enumerate(terms):self.write(term,project='project_'+str(n))
        request,_,_=self.request(' '.join(terms),[])
        block=self.uncertainty(request)[0]
        self.assertTrue(block['ambiguous']);self.assertEqual(len(block['candidates']),8)
        self.assertLessEqual(len(json.dumps(block,ensure_ascii=False)),2000)

    def test_shared_subject_is_ambiguous_even_when_self_cites_only_one(self):
        one=self.write('На маяке смотритель оставил фонарь.',project='north')
        self.write('На маяке рыбак починил ступень.',project='south')
        self.discuss(one)
        request,_,_=self.request('Вернусь к маяку.',[])
        block=self.uncertainty(request)[0]
        self.assertTrue(block['ambiguous'])
        self.assertEqual({c['project_id'] for c in block['candidates']},{'north','south'})
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))

    def test_comparison_preserves_shared_topic_but_specific_modifier_can_disambiguate(self):
        one=self.write('Северный маяк на скале.',project='north')
        self.write('Южный маяк на острове.',project='south')
        rocket=self.write('Ракета летела.',project='rocket');self.discuss(one)
        request,_,_=self.request('Сравним маяк и ракету.',[rocket])
        self.assertEqual({c['project_id'] for c in self.uncertainty(request)[0]['candidates']},{'north','south','rocket'})
        request,_,_=self.request('Продолжим северный маяк.',[rocket])
        self.assertEqual(self.catalog(request)['world_results'][0]['project_id'],'north')
        self.assertFalse(self.uncertainty(request))
