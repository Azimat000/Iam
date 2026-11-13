import json
from unittest.mock import patch
from iam.stubs import Reply, rest
from iam.world.recent import recent_work
from iam.world.cycle import LivingCycle, GUIDANCE
from social.test_context_repair import ContextCase
from world.helpers import LivingCase


class AutomaticRecollectionTest(ContextCase):
    def catalog(self, request):
        return next(json.loads(i['text']) for i in request['context'] if i['text'].startswith('{"library"'))

    def test_complete_small_work_occurs_once_with_source_and_limit(self):
        body = 'Обсерватория. ' + 'Тёплый свет на медном куполе. ' * 16 + 'ЗАКЛЮЧИТЕЛЬНАЯ_СТРОКА'
        row = self.write(body, project='observatory')
        for records in ([], [row]):
            request, _, _ = self.request('Поговорим об обсерватории.', records)
            result = self.catalog(request)['world_results'][0]
            self.assertEqual(result['text'], body)
            self.assertTrue(result['result_complete'])
            self.assertIn(result['event_id'], row.sources)
            blocks = [i for i in request['context'] if i['ref'] == row.memory_id]
            for block in blocks:
                packet = json.loads(block['text'])
                self.assertEqual(packet['content_in'], 'world_results')
                self.assertNotIn('text', packet)
            self.assertEqual(json.dumps(request, ensure_ascii=False).count('ЗАКЛЮЧИТЕЛЬНАЯ_СТРОКА'), 1)
        extra, result = recent_work(self.m, self.world.state, [])
        self.assertLessEqual(len(extra[0]['text']) + len(json.dumps(result, ensure_ascii=False)), 2000)
        self.assertNotIn('перелистывания', GUIDANCE)
        self.assertNotIn('Чтобы открыть', GUIDANCE)

    def test_long_work_is_automatic_honest_prefix_and_limit(self):
        body = 'Начало. ' * 500 + 'НЕИЗВЕСТНЫЙ_ХВОСТ'
        row = self.write(body, project='long')
        extra, result = recent_work(self.m, self.world.state, [])
        self.assertFalse(result['result_complete'])
        self.assertTrue(body.startswith(result['text']))
        self.assertTrue(result['result_window']['more'])
        self.assertEqual(result['text'], row.text[result['result_window']['start']:result['result_window']['end']])
        self.assertLessEqual(len(extra[0]['text']) + len(json.dumps(result, ensure_ascii=False)), 2000)
        self.assertNotIn('НЕИЗВЕСТНЫЙ_ХВОСТ', str((extra, result)))

    def test_final_request_long_work_has_single_index_and_shared_budget(self):
        row = self.write('Обсерватория. ' * 500, project='observatory')
        request, _, _ = self.request('Вернёмся к обсерватории.', [])
        result = self.catalog(request)['world_results'][0]
        blocks = [i for i in request['context'] if i['ref'] == row.memory_id]
        self.assertEqual(len(blocks), 1)
        self.assertLessEqual(len(blocks[0]['text']) + len(json.dumps(result, ensure_ascii=False)), 2000)
        from iam.memory.window import next_window
        self.assertEqual(next_window(row, blocks), result['result_window']['end'])

    def test_maximum_work_remains_fully_readable_in_existing_catalog_channel(self):
        body = 'А' * 7990 + 'ПОСЛЕДНИЕ!'
        self.assertEqual(len(body), 8000)
        row = self.write(body, project='maximum')
        request, _, _ = self.request('Покажи работу.', [row])
        result = next(r for r in self.catalog(request)['world_results'] if r['memory_id'] == row.memory_id)
        self.assertEqual(result['text'], body)
        self.assertTrue(result['result_complete'])
        from iam.memory.window import next_window
        self.assertEqual(next_window(row, request['context']), 0)

    def test_rejects_malformed_unverifiable_result_and_unsafe_index(self):
        from dataclasses import replace
        row = self.write('Тестовая работа', project='test')
        with patch.object(self.m, 'records', return_value=[replace(row, content={**row.content, 'text': 'Подмена'})]):
            self.assertEqual(recent_work(self.m, self.world.state, []), ([], None))
        with patch('iam.world.recent.result_view', return_value=None):
            self.assertEqual(recent_work(self.m, self.world.state, []), ([], None))
        with patch.object(self.m, '_safe_item', side_effect=[True, False]):
            self.assertEqual(recent_work(self.m, self.world.state, []), ([], None))

    def test_forgotten_latest_off_and_unsafe_content_are_not_recovered(self):
        self.write('Старый купол', project='sky')
        row = self.write('Новый купол', project='sky')
        self.assertTrue(self.m.fade(row.memory_id).applied)
        self.assertEqual(recent_work(self.m, self.world.state, []), ([], None))
        self.m.off = True
        self.assertEqual(recent_work(self.m, self.world.state, []), ([], None))
        self.m.off = False
        self.write('Другая работа', project='other')
        with patch.object(self.m, '_safe_item', return_value=False):
            self.assertEqual(recent_work(self.m, self.world.state, []), ([], None))


class ReplaySchedulingTest(LivingCase):
    def test_replayed_triggers_schedule_once_per_epoch_and_new_triggers_still_schedule(self):
        trigger = self.due()
        self.p.queue(Reply((rest(),)))
        self.cycle.run(trigger)
        del self.cycle._scheduled_epoch
        with patch.object(self.cycle, '_schedule_intentions', wraps=self.cycle._schedule_intentions) as schedule:
            for _ in range(12):
                self.assertEqual(self.cycle.run(trigger).reason, 'already_done')
            self.assertEqual(schedule.call_count, 1)
            self.c.pause_session('operator'); self.c.resume_session()
            self.assertEqual(self.cycle.run(trigger).reason, 'already_done')
            self.assertEqual(schedule.call_count, 2)
            self.p.queue(Reply((rest(),)))
            self.assertEqual(self.cycle.run(self.due()).outcome, 'done')
            self.assertGreater(schedule.call_count, 2)

    def test_replay_after_reconstruction_restores_missing_moment_without_provider(self):
        source = self.read()
        goal = self.goal(source)
        goal['body']['not_before_life_ms'] = self.c.life_time_ms + 100000
        trigger = self.due()
        self.p.queue(Reply((goal,)))
        with patch.object(self.cycle, '_schedule_intentions'):
            self.assertEqual(self.cycle.run(trigger).outcome, 'done')
        mid = self.c.state['intentions']['my_goal']['moment_id']
        self.assertNotIn(mid, self.c.state['world_time']['moments'])
        fresh = LivingCycle(self.c, self.p, self.m, self.world, self.caps, self.cycle.creator)
        before = len(self.p.calls)
        self.assertEqual(fresh.run(trigger).reason, 'already_done')
        self.assertIn(mid, self.c.state['world_time']['moments'])
        self.assertEqual(len(self.p.calls), before)
        from iam.state import verify
        self.assertEqual(verify(self.c.store), [])
