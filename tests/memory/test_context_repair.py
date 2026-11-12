from social.test_context_repair import ContextCase


class MemoryWindowContextTest(ContextCase):
    def test_old_personal_window_identifies_own_unknown_words(self):
        source, answer = self.exchange('Что скажешь о поездке?', 'Пока не помню подробностей.', outcome='unknown')
        hits = self.distract()
        request, _, _ = self.request('Что было сказано о поездке?', [answer, source, *hits[:3]])
        packet = next(packet for packet in self.packets(request, answer.memory_id) if 'text_window' in packet)
        self.assertEqual(packet.get('speaker'), 'self')
        self.assertEqual(packet['outcome'], 'unknown')
        self.assertEqual(packet['text'], answer.text)

    def test_result_views_reject_unverified_or_malformed_windows(self):
        import copy
        import json
        from dataclasses import replace
        from iam.world.recent import result_view, recent_work
        row = self.write('Проверенный небольшой текст', project='paper')
        state = self.world.state
        good = {'ref': row.memory_id, 'layer': row.layer, 'text': self.m._window(row)}
        packet = json.loads(good['text'])
        malformed = ['not json', None, '[]', json.dumps({'version': 99}),
                     json.dumps({**packet, 'text_window': []}),
                     json.dumps({**packet, 'text_window': {'start': 0, 'end': len(row.text)+1}}),
                     json.dumps({**packet, 'text': 'ПОДМЕНЁННЫЙ ТЕКСТ'}),
                     json.dumps({**packet, 'text': row.text[:3], 'text_window': {'start': 0, 'end': 3}})]
        for value in malformed:
            with self.subTest(value=value):
                self.assertIsNone(result_view(self.m, row, [{**good, 'text': value}], state))
        self.assertIsNone(result_view(self.m, replace(row, version=2), [good], state))
        self.assertIsNone(result_view(self.m, replace(row, origin='own_hypothesis'), [good], state))
        self.assertIsNone(result_view(self.m, replace(row, content={**row.content, 'text': 'Непроверенная цитата'}), [good], state))
        for source in ('evt_MissingEvent01', self.root(self.c)):
            bad_state = copy.deepcopy(state)
            bad_state['actions'][row.content['action_id']]['receipt']['event_id'] = source
            broken = replace(row, sources=(source,))
            self.assertIsNone(result_view(self.m, broken, [good], bad_state))
            self.assertEqual(recent_work(self.m, bad_state, []), ([], None))
        for limit in (450, 500, 550, 600, 650, 700, 750, 800):
            self.m.text_limit = limit
            items, result = recent_work(self.m, state, [])
            if result is not None:
                self.assertTrue('Проверенный небольшой текст'.startswith(result['text']))
                self.assertLessEqual(len(items[0]['text']) + len(json.dumps(result, ensure_ascii=False)), 2000)

    def test_safety_filter_is_applied_to_added_work_and_neighbor_windows(self):
        from unittest.mock import patch
        from iam.social.context import neighbor_context
        from iam.world.recent import recent_work
        source, answer = self.exchange('Какое предложение напишем?', 'ЗАПРЕЩЁННЫЙ_МАРКЕР')
        self.distract()
        current, trigger = self.incoming('Вернёмся к предложению.')
        latest = self.write('ЗАПРЕЩЁННЫЙ_МАРКЕР', project='paper')
        with patch.object(self.m, '_blocked', return_value=['ЗАПРЕЩЁННЫЙ_МАРКЕР']):
            self.assertEqual(neighbor_context(self.m, trigger, [source], []), [])
            self.assertEqual(recent_work(self.m, self.world.state, []), ([], None))
            shown = [{'ref': latest.memory_id, 'layer': latest.layer, 'text': self.m._window(latest)}]
            self.assertEqual(recent_work(self.m, self.world.state, shown), ([], None))

    def test_work_windows_do_not_rescan_dialogue_for_authorship(self):
        from unittest.mock import patch
        from iam.world.recent import recent_work
        latest = self.write('Текст отдельной работы', project='paper')
        with patch.object(self.m, '_turns', side_effect=AssertionError('Work is not a dialogue turn')):
            items, result = recent_work(self.m, self.world.state, [])
        self.assertEqual(result['memory_id'], latest.memory_id)
        self.assertTrue(items)
