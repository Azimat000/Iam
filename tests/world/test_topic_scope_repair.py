import json
from unittest.mock import patch
from social.test_context_repair import ContextCase
from world import test_source_project_repair as source_cases
from iam.world.subject import subject_scopes, subject_match
from iam.world.discussion import discussion_work


class TopicScopeRepairTest(ContextCase):
    catalog = staticmethod(source_cases.SourceProjectRepairTest.catalog)
    discuss = source_cases.SourceProjectRepairTest.discuss

    def blocks(self, request):
        return [json.loads(i['text']) for i in request['context'] if '"kind": "discussion_project"' in i['text']]

    def test_greeting_and_editing_verb_do_not_create_false_candidates(self):
        wrong=self.write('Здравствуйте. В комнате осталась только тень.',project='greeting')
        target=self.write('Телескоп отражал звёзды.',project='telescope')
        for phrase in ('Здравствуйте! Вернусь к сцене про телескоп: что в ней осталось?',
                       'Хочу поговорить о телескопе. Что ты оставила бы без изменений?',
                       'Что осталось в сцене про телескоп? Здравствуйте, между прочим.'):
            request,current,_=self.request(phrase,[wrong])
            result=self.catalog(request)['world_results'][0]
            self.assertEqual(result['project_id'],'telescope',phrase)
            self.assertEqual(result['memory_id'],target.memory_id)
            self.assertEqual(result['discussion_ref'],current.memory_id)
            self.assertFalse(self.blocks(request))

    def test_independent_subjects_and_inflected_procedural_words(self):
        noise=self.write('Добрый день! Она хотела поменять оставшиеся вещи.',project='noise')
        for project,body,query in (
            ('orchid','Орхидея стояла у окна.','Добрый день. Оставшиеся детали — потом. Расскажи про орхидею: что хотелось поменять?'),
            ('compass','Компас лежал на карте.','Она хотела поменять тему. Вернусь к компасу: что оставалось?'),
            ('violin','Скрипка лежала в футляре.','Что осталось в сцене со скрипкой? Добрый день!')):
            target=self.write(body,project=project)
            request,_,_=self.request(query,[noise])
            self.assertEqual(self.catalog(request)['world_results'][0]['memory_id'],target.memory_id)
            self.assertFalse(self.blocks(request))

    def test_unknown_explicit_subject_never_falls_back_to_self_cites(self):
        wrong=self.write('Старый якорь.',project='anchor');self.discuss(wrong)
        request,current,trigger=self.request('Вернусь к сцене про метеорит.',[wrong])
        block=self.blocks(request)[0]
        self.assertTrue(block['unresolved']);self.assertEqual(block['message_ref'],current.memory_id)
        self.assertFalse(block['available_current_result'])
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
        with patch.object(self.m,'_safe_item',return_value=False):
            self.assertEqual(discussion_work(self.m,self.world.state,self.m.dialogue(trigger)),([],None))

    def test_two_explicit_clauses_and_nested_comparison_keep_real_ambiguity(self):
        one=self.write('Лодка плыла по озеру.',project='boat')
        two=self.write('Дирижабль летел над городом.',project='airship')
        self.discuss(one)
        for query in ('Поговорим о лодке. И вернёмся к дирижаблю.',
                      'Сравним лодку и сцену про дирижабль.'):
            request,_,_=self.request(query,[two])
            block=self.blocks(request)[0]
            self.assertTrue(block['ambiguous'])
            self.assertEqual({c['project_id'] for c in block['candidates']},{'boat','airship'})

    def test_same_subject_different_works_and_named_modifier(self):
        one=self.write('Белая башня у моря.',project='white')
        two=self.write('Чёрная башня в лесу.',project='black');self.discuss(one)
        request,_,_=self.request('Приветствую. Про башню: что поменялось?', [two])
        self.assertTrue(self.blocks(request)[0]['ambiguous'])
        request,_,_=self.request('Про белую башню: что оставалось?', [two])
        self.assertEqual(self.catalog(request)['world_results'][0]['project_id'],'white')

    def test_hidden_latest_and_revised_source_remain_unavailable(self):
        old=self.write('Орган в пустой церкви.',project='organ')
        latest=self.write('Орган: СКРЫТЫЙ_ФИНАЛ',project='organ')
        self.assertTrue(self.m.fade(latest.memory_id).applied)
        request,_,_=self.request('Рассмотрим сцену про орган.',[old])
        self.assertFalse(self.blocks(request)[0]['available_current_result'])
        self.assertNotIn('СКРЫТЫЙ_ФИНАЛ',json.dumps(request,ensure_ascii=False))
        self.assertTrue(self.m.revise(old.memory_id,'ЗАМЕНА',sources=old.sources,reason='Поправка').applied)
        request,_,_=self.request('Расскажи про орган.',[])
        self.assertTrue(self.blocks(request)[0]['unresolved'])
        self.m.off=True
        self.assertEqual(discussion_work(self.m,self.world.state,[]),([],None))

    def test_foreign_and_unread_subject_cannot_link(self):
        work=self.write('Обелиск стоял в пустыне.',project='obelisk')
        foreign,_=self.incoming('Поговорим про обелиск.',channel='email',sender='other')
        other=self.write('Фонарь горел.',project='lamp')
        request,_,_=self.request('Каков последний вариант?',[other,foreign])
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))
        _,trigger=self.incoming('Вернусь к обелиску.',read=False)
        self.assertEqual(discussion_work(self.m,self.world.state,self.m.dialogue(trigger)),([],None))
        self.assertTrue(self.m.fade(work.memory_id).applied)

    def test_scope_grammar_boundaries_and_english(self):
        self.assertEqual(subject_scopes('About a lighthouse: what remains?'),[{'lighthouse'}])
        self.assertEqual(subject_scopes('Compare lighthouse and rocket.'),[{'lighthouse','rocket'}])
        self.assertEqual(subject_scopes('Про маяк, который стоял у моря.'),[{'маяк'}])
        self.assertIsNone(subject_scopes('Каков последний вариант?'))
        self.assertIsNone(subject_scopes('Вернусь к прежней сцене.'))

    def test_regular_adjective_endings_do_not_enable_short_noun_prefixes(self):
        self.assertTrue(subject_match('белую','белая',anchored=True))
        self.assertTrue(subject_match('синий','синяя',anchored=True))
        self.assertTrue(subject_match('маяку','маяке'))
        for a,b in (('белую','чёрная'),('дом','домик'),('camel','камень'),('ракету','rocket')):
            self.assertFalse(subject_match(a,b))

    def test_referenced_subject_in_procedural_question_does_not_add_project(self):
        noise=self.write('После изменений осталась лишь тень.',project='noise')
        target=self.write('Телескоп отражал звёзды.',project='telescope')
        request,_,_=self.request('Поговорим о телескопе. Что ты оставила бы в нём без изменений?',[noise])
        self.assertFalse(self.blocks(request))
        self.assertEqual(self.catalog(request)['world_results'][0]['memory_id'],target.memory_id)

    def test_specific_modifier_does_not_narrow_independent_second_phrase(self):
        one=self.write('Белая башня у моря.',project='white')
        two=self.write('Чёрная башня в лесу.',project='black')
        request,_,_=self.request('Поговорим о белой башне. И ещё про башню.',[two])
        block=self.blocks(request)[0]
        self.assertTrue(block['ambiguous'])
        self.assertEqual({c['project_id'] for c in block['candidates']},{'white','black'})
        request,_,_=self.request('Поговорим о белой башне. И ещё про неизвестный метеорит.',[one])
        self.assertTrue(self.blocks(request)[0]['unresolved'])
        self.assertFalse(any(r.get('discussion_ref') for r in self.catalog(request)['world_results']))

    def test_suffix_only_noun_adverb_pair_cannot_add_a_project(self):
        self.assertFalse(subject_match('строй','строго',anchored=True))
        self.assertFalse(subject_match('белую','белая'))
        formation=self.write('Строй двигался к площади.',project='formation')
        noise=self.write('Она строго посмотрела на него.',project='glance')
        request,_,_=self.request('Поговорим про строй.',[noise])
        self.assertFalse(self.blocks(request))
        self.assertEqual(self.catalog(request)['world_results'][0]['memory_id'],formation.memory_id)
