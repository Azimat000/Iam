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
