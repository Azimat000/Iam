import json
from unittest.mock import patch
from iam.stubs import Reply, rest
from social.test_context_repair import ContextCase


class WorkContextRepairTest(ContextCase):
    def test_latest_version_is_shown_even_when_retrieval_contains_old_work(self):
        oldest = self.write('НАБРОСОК: безымянная башня.', project='scene')
        self.time.advance(1000)
        old = self.write('ЧЕРНОВИК: башня и дождь.', project='scene')
        self.time.advance(1000)
        latest = self.write('НОВАЯ СЦЕНА: смотритель закрыл окно, оставив лампу у двери.', project='scene')
        hits = self.distract()
        from iam.social import SocialMemory
        from iam.world import PersistentWorld
        from iam.world.cycle import LivingCycle
        from iam.stubs import StubWorld, StubCreatorBridge
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        self.m = SocialMemory(self.c)
        self.world = PersistentWorld(self.c, external=StubWorld())
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.c.resume_session()
        initial = [oldest, old, *hits[:3]]
        request, _, _ = self.request('Как получилось? Покажи последнюю работу.', initial)
        self.assertTrue({row.memory_id for row in initial} <= {item['ref'] for item in request['context']})
        latest_packet = next(packet for packet in self.packets(request, latest.memory_id)
                             if packet.get('recall_role') == 'recent_activity')
        self.assertTrue(latest_packet['current_work'])
        self.assertEqual(latest_packet['content_in'], 'world_results')
        self.assertNotIn('text', latest_packet)
        catalog = next(json.loads(item['text']) for item in request['context'] if item['text'].startswith('{"library"'))
        self.assertEqual(catalog['world_results'][0]['memory_id'], latest.memory_id)
        self.assertEqual(catalog['world_results'][0]['text'], 'НОВАЯ СЦЕНА: смотритель закрыл окно, оставив лампу у двери.')

    def test_latest_hidden_or_revised_never_promotes_an_older_version(self):
        old = self.write('РАННЯЯ_ВЕРСИЯ', project='scene')
        self.time.advance(1000)
        latest = self.write('НОВАЯ_СКРЫТАЯ_ВЕРСИЯ', project='scene')
        hits = self.distract()
        self.assertTrue(self.m.fade(latest.memory_id).applied)
        request, _, _ = self.request('Как получилось?', [old, *hits[:4]])
        self.assertNotIn('НОВАЯ_СКРЫТАЯ_ВЕРСИЯ', json.dumps(request, ensure_ascii=False))
        self.assertFalse(any('"current_work": true' in item['text'] for item in request['context']))
        self.time.advance(1000)
        revised = self.write('ТЕКСТ_ДО_ПЕРЕСМОТРА', project='scene')
        self.assertTrue(self.m.revise(revised.memory_id, 'Исправленная память', sources=revised.sources, reason='Правка').applied)
        request, _, _ = self.request('Последняя работа?', [old, *hits[:4]])
        self.assertNotIn('ТЕКСТ_ДО_ПЕРЕСМОТРА', json.dumps(request, ensure_ascii=False))
        self.assertFalse(any('"current_work": true' in item['text'] for item in request['context']))

    def test_work_and_catalog_share_budget_and_never_hydrate_unshown_tail(self):
        old = self.write('Прежняя заметка', project='scene')
        self.time.advance(1000)
        latest = self.write('Сцена у окна. ' * 400 + 'НЕПОКАЗАННЫЙ_КОНЕЦ_РАБОТЫ', project='scene')
        hits = self.distract()
        self.m.text_limit = 6000
        request, _, _ = self.request('Как получилось?', [old, *hits[:4]])
        recent = next(item for item in request['context'] if '"recall_role": "recent_activity"' in item['text'])
        catalog = next(json.loads(item['text']) for item in request['context'] if item['text'].startswith('{"library"'))
        result = catalog['world_results'][0]
        self.assertEqual(result['memory_id'], latest.memory_id)
        self.assertLessEqual(len(recent['text']) + len(json.dumps(result, ensure_ascii=False)), 2000)
        self.assertFalse(result['result_complete'])
        self.assertNotIn('НЕПОКАЗАННЫЙ_КОНЕЦ_РАБОТЫ', json.dumps(request, ensure_ascii=False))
        packet = json.loads(recent['text'])
        window = result['result_window']
        self.assertEqual(result['text'], latest.text[window['start']:window['end']])
        self.assertEqual(packet['content_in'], 'world_results')
        self.assertNotIn('text', packet)
        self.m.text_limit = 100
        request, _, _ = self.request_small('Как получилось?')
        self.assertFalse(any('"recall_role": "recent_activity"' in item['text'] for item in request['context']))
        self.m.off = True
        self.p.queue(Reply((rest(),)))
        self.cycle.run(self.due())
        self.assertNotIn('Сцена у окна', json.dumps(self.p.calls[-1], ensure_ascii=False))

    def request_small(self, text):
        _, trigger = self.incoming(text)
        self.p.queue(Reply((rest(),)))
        self.cycle.run(trigger)
        return self.p.calls[-1], None, trigger

    def test_catalog_does_not_read_text_from_world_and_complete_visible_work_can_finish(self):
        from unittest.mock import PropertyMock
        from iam.world import PersistentWorld
        source = self.read()
        self.run_reply(self.goal(source, project='scene'))
        latest = self.write('Свет у окна.\n' + 'Вторая строка о свете. ' * 30, project='scene')
        state = self.world.state
        state['actions'][latest.content['action_id']]['outcome'] = 'ЗАПРЕЩЁННОЕ_ТЕЛО_ИЗ_WORLD'
        state['works'][latest.content['action_id']]['text'] = 'ЗАПРЕЩЁННОЕ_ТЕЛО_ИЗ_WORKS'
        with patch.object(PersistentWorld, 'state', new_callable=PropertyMock, return_value=state):
            request, _, _ = self.request('Покажи работу.', [latest, source])
        blob = json.dumps(request, ensure_ascii=False)
        self.assertNotIn('ЗАПРЕЩЁННОЕ_ТЕЛО', blob)
        catalog = next(json.loads(item['text']) for item in request['context'] if item['text'].startswith('{"library"'))
        result = next(item for item in catalog['world_results'] if item['memory_id'] == latest.memory_id)
        self.assertTrue(result['result_complete'])
        self.assertIn('Вторая строка', result['text'])
        self.run_reply(self.transition('done', result=latest), replies=(self.assessment(), self.assessment('evaluation')))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'done')

    def test_unknown_attempt_is_not_a_confirmed_work(self):
        from iam.interfaces import ActionOutcome
        from iam.stubs import act
        from iam.world import WRITE
        previous = self.write('Подтверждённый вариант', project='scene')
        with patch.object(self.world, 'execute', return_value=ActionOutcome('unknown', 'НЕОПРЕДЕЛЁННЫЙ_НОВЫЙ_ВАРИАНТ')):
            report = self.run_reply(act(WRITE, {'project_id': 'scene', 'title': 'Проба', 'text': 'Неопределённая запись'}))
        unknown = self.c.store.get_memory(report.actions[0].memory_id)
        request, _, _ = self.request('Как получилось?', [unknown, previous])
        catalog = next(json.loads(item['text']) for item in request['context'] if item['text'].startswith('{"library"'))
        self.assertNotIn(unknown.memory_id, {item['memory_id'] for item in catalog['world_results']})
        self.assertEqual(catalog['world_results'][0]['memory_id'], previous.memory_id)

    def test_long_visible_results_are_presented_fully_from_memory_not_world(self):
        from unittest.mock import PropertyMock
        from iam.world import PersistentWorld, READ
        from iam.stubs import act
        self.c.pause_session('operator')
        material_text = 'Подробности наблюдения. ' * 300 + 'КОНЕЦ_МАТЕРИАЛА'
        material = self.world.add_material('Наблюдение', material_text, 'Проверка полного результата')
        self.c.resume_session()
        report = self.run_reply(act(READ, {'material_id': material}))
        reading = self.c.store.get_memory(report.actions[0].memory_id)
        work_text = 'Записи у реки. ' * 450 + 'КОНЕЦ_РАБОТЫ'
        work = self.write(work_text, project='river')
        state = self.world.state
        for row in (reading, work):
            state['actions'][row.content['action_id']]['outcome'] = 'НЕ_БРАТЬ_ИЗ_WORLD'
        state['works'][work.content['action_id']]['text'] = 'НЕ_БРАТЬ_ИЗ_WORKS'
        with patch.object(PersistentWorld, 'state', new_callable=PropertyMock, return_value=state):
            request, _, _ = self.request('Покажи доступные результаты.', [reading, work])
        self.assertNotIn('НЕ_БРАТЬ_ИЗ_', json.dumps(request, ensure_ascii=False))
        catalog = next(json.loads(item['text']) for item in request['context'] if item['text'].startswith('{"library"'))
        self.assertLessEqual(len(catalog['world_results']), 3)
        for row, expected in ((reading, material_text), (work, work_text)):
            result = next(item for item in catalog['world_results'] if item['memory_id'] == row.memory_id)
            self.assertEqual(result['text'], expected)
            self.assertEqual(result['result_presentation'], 'catalog_window')
            self.assertTrue(result['result_complete'])
            window = result['result_window']
            self.assertEqual(result['text'], row.text[window['start']:window['end']])
            self.assertFalse(window['more'])
            personal = next(packet for packet in self.packets(request, row.memory_id) if 'text_window' in packet)
            self.assertEqual(personal['content_in'], 'world_results')
            self.assertEqual(personal['text_window'], result['result_window'])
            self.assertNotIn('text', personal)
        with patch.object(self.m, '_blocked', return_value=['КОНЕЦ_РАБОТЫ', 'КОНЕЦ_МАТЕРИАЛА']):
            request, _, _ = self.request('Покажи доступные результаты.', [reading, work])
        self.assertNotIn('КОНЕЦ_РАБОТЫ', json.dumps(request, ensure_ascii=False))
        self.assertNotIn('КОНЕЦ_МАТЕРИАЛА', json.dumps(request, ensure_ascii=False))
