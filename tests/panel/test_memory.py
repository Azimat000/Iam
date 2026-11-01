"""Страница постоянной памяти: происхождение, история, изоляция и безопасный HTML."""
import unittest
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from iam.domain import MEMORY_KINDS
from iam.panel.memory import KINDS, ORIGINS, render_memory
from iam.panel.app import create_app
from iam.panel.runtime import Runtime
from state.helpers import memory


class MemoryPageTest(unittest.TestCase):
    def test_empty_and_archive_only(self):
        for records in ([], [memory('Секрет архива', layer='archive')]):
            html = render_memory(records)
            self.assertIn('Записей памяти пока нет', html)
            self.assertNotIn('Секрет архива', html)

    def test_nine_kinds_provenance_and_history_are_visible(self):
        records = []
        for i, kind in enumerate(MEMORY_KINDS):
            row = memory('Текст памяти ' + str(i), origin='observation')
            row.update(kind=kind, history=[{'version': 1, 'at_seq': 3, 'change': 'создана'}])
            records.append(row)
        html = render_memory(records)
        for label in KINDS.values():
            self.assertIn(label, html)
        for field in ('Наблюдение', 'Источники, связи и история', '2026-10-11', 'создана', 'evt_ABCDEFGH'):
            self.assertIn(field, html)
        self.assertEqual(html.count('<article'), 9)

    def test_all_origins_and_statement_warning(self):
        for origin, label in ORIGINS.items():
            row = memory('Пример', origin=origin)
            html = render_memory([row])
            self.assertIn(label, html)
            self.assertEqual('Истинность его содержания' in html, origin == 'stated_by_other')
        row = memory('Человек сообщил', origin='observation', claim_kind='statement_made',
                     independence_group='person:one')
        self.assertIn('Истинность его содержания', render_memory([row]))

    def test_escapes_text_source_history_and_interpretation(self):
        row = memory('<script>опасно</script>', origin='own_hypothesis', layer='interpretation')
        row['source']['ref'] = '<img src=x onerror=alert(1)>'
        row['history'] = [{'version': 1, 'at_seq': 3, 'change': '</pre><script>bad</script>'}]
        html = render_memory([row])
        self.assertIn('Интерпретация', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)

    def test_http_uses_persistent_memory_without_writing_on_read(self):
        from iam.memory import PersistentMemory
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory) / 'demo.db', demo=True)
            with TestClient(create_app(runtime, port=18765), base_url='http://127.0.0.1:18765') as client:
                self.assertIn('Записей памяти пока нет', client.get('/memory').text)
                def seed():
                    self.assertIsInstance(runtime.cycle.memory, PersistentMemory)
                    self.assertIn('memory_write', runtime.cycle.allowed_proposals)
                    ctl = runtime.ctl
                    ctl.start_session()
                    rec = runtime.cycle.memory.prepare(kind='semantic', origin='own_hypothesis',
                            text='Проверочная гипотеза <script>bad</script>',
                            sources=(ctl.store.get_event(1).event_id,))
                    self.assertTrue(runtime.cycle.memory.write(rec).applied)
                    ctl.stop_session()
                    return ctl.state_version
                runtime._test_seed_memory = seed
                version = runtime.submit('_test_seed_memory').result(5)
                page = client.get('/memory')
                self.assertEqual(page.status_code, 200)
                self.assertIn('Проверочная гипотеза &lt;script&gt;', page.text)
                self.assertIn('Собственная гипотеза', page.text)
                self.assertIn('Источники, связи и история', page.text)
                self.assertEqual(runtime.submit('view').result(5)['version'], version)

class MemoryRestoreHTTPTest(unittest.TestCase):
    def setUp(self):
        import re
        self.re = re
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Runtime(Path(self.directory.name) / 'demo.db', demo=True, interval=100)
        self.client = TestClient(create_app(self.runtime, port=18765), base_url='http://127.0.0.1:18765')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        def seed():
            ctl, m = self.runtime.ctl, self.runtime.cycle.memory
            ctl.start_session()
            r = m.prepare(kind='semantic', origin='own_hypothesis', text='Деталь возвращена <script>опасно</script>',
                          sources=(ctl.store.get_event(1).event_id,))
            self.assertTrue(m.write(r).applied)
            self.assertTrue(m.fade(r.memory_id).applied)
            ctl.pause_session('operator')
            return r.memory_id
        self.runtime._test_seed_restore = seed
        self.mid = self.runtime.submit('_test_seed_restore').result(5)

    def fields(self):
        html = self.client.get('/memory').text
        return {name:self.re.search('name="' + name + '" value="([^"]*)"', html).group(1)
                for name in ('csrf','epoch','version')}

    def post(self, **kw):
        data = {**self.fields(), 'memory_id':self.mid, 'memory_version':'1', 'reason':'вернуть точную запись', 'confirm':'memory', **kw}
        return self.client.post('/actions/memory_restore', data=data, headers={'Origin':'http://127.0.0.1:18765'})

    def test_restore_success_and_rejections_preserve_fields(self):
        page = self.client.get('/memory')
        self.assertIn('Восстановить запись памяти', page.text)
        for kwargs in ({'confirm':''}, {'memory_version':'нет'}, {'memory_version':'999'}, {'reason':' '}, {'version':'1'}):
            r = self.post(**kwargs)
            self.assertEqual(r.status_code, 409)
            self.assertIn('Восстановить запись из архива', r.text)
        self.assertEqual(self.post(csrf='wrong').status_code, 403)
        before = self.runtime.submit('view').result(5)
        r = self.post()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('Запись восстановлена в личную память', r.text)
        self.assertIn('Восстановлена оператором из архива', r.text)
        self.assertNotIn('<script>', r.text)
        after = self.runtime.submit('view').result(5)
        restored = [m for m in after['memories'] if m.get('restored_by_operator')]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]['origin'], 'own_hypothesis')
        self.assertEqual(after['version'], before['version'] + 1)
        events = [e for e in after['events'] if e['type'] == 'operator.intervention' and e['payload'].get('kind') == 'restore_from_archive']
        self.assertEqual(len(events), 1)
        self.assertEqual(self.client.get('/memory').status_code, 200)
        self.assertEqual(self.runtime.submit('view').result(5)['version'], after['version'])

    def test_running_inflight_and_failed_restore(self):
        def state(running=False, flight=False):
            if running:
                self.runtime.ctl.resume_session()
            elif self.runtime.ctl.session_state == 'running':
                self.runtime.ctl.pause_session('operator')
            self.runtime.session.in_flight = flight
        self.runtime._test_restore_state = state
        self.runtime.submit('_test_restore_state', True).result(5)
        page = self.client.get('/memory').text
        self.assertNotIn('/actions/memory_restore', page)
        # Версионные поля доступны через главную страницу, отправка вручную тоже запрещена.
        html = self.client.get('/').text
        fields = {name:self.re.search('name="' + name + '" value="([^"]*)"', html).group(1) for name in ('csrf','epoch','version')}
        r = self.client.post('/actions/memory_restore', data={**fields,'reason':'тест','confirm':'memory','memory_id':self.mid,'memory_version':'1'}, headers={'Origin':'http://127.0.0.1:18765'})
        self.assertEqual(r.status_code, 409)
        self.runtime.submit('_test_restore_state', False, True).result(5)
        self.assertNotIn('/actions/memory_restore', self.client.get('/memory').text)
        self.runtime.submit('_test_restore_state', False, False).result(5)
        from unittest.mock import patch
        from iam.state import ApplyResult
        with patch.object(self.runtime.cycle.memory, 'restore_from_archive', return_value=(None, ApplyResult('rejected',1,1))):
            self.assertEqual(self.post().status_code, 409)
