"""Результаты доступны без запуска личности; полный текст не подменяет отдельный эпизод."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from iam.panel.app import create_app
from iam.panel.experiments import MAX_BYTES, SNAPSHOT, load_archive, render_archive, text_blocks, url
from iam.panel.runtime import Runtime
from iam.panel.views import render

ROOT = Path(__file__).resolve().parents[2]


def small_archive():
    return {'schema': 1, 'series': [{'id': 'first', 'title': 'Проверка', 'description': 'Описание',
        'metrics': [['Прогонов', 1]], 'findings': ['Есть ограничение'], 'stories': [
            {'id': 'D001', 'moments': [{'number': 1, 'session': 1, 'body': 'Первый текст'},
                                     {'number': 2, 'session': 1, 'body': 'Последний текст'}]}]}]}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'archive.json'

    def load(self, data):
        self.path.write_text(json.dumps(data))
        return load_archive(self.path)

    def test_packaged_archive_reproduces_published_sources_and_has_all_14_histories(self):
        builder = runpy.run_path(str(ROOT / 'scripts/experiments/build_panel_snapshot.py'))
        data = load_archive()
        self.assertEqual(data, builder['build'](ROOT))
        self.assertLess(SNAPSHOT.stat().st_size, MAX_BYTES)
        for name, digest in data['sources'].items():
            self.assertEqual(hashlib.sha256((ROOT / 'docs/experiments' / name).read_bytes()).hexdigest(), digest)
        series = {s['id']: s for s in data['series']}
        self.assertEqual([len(s['stories']) for s in data['series']], [12, 0, 0, 2])
        self.assertEqual(dict(series['motivation']['metrics'])['Прогонов'], 84)
        self.assertEqual(dict(series['agency']['metrics'])['Внешних попыток модели'], 54)
        for entry in data['series']:
            for story in entry['stories']:
                self.assertEqual(len(story['moments']), 18)
                self.assertEqual({m['session'] for m in story['moments']}, set(range(1, 7)))

    def test_missing_and_empty_archives_have_an_honest_empty_state(self):
        self.assertEqual(load_archive(self.path), {'series': []})
        self.assertIn('пока не установлен', render_archive(self.load({'schema': 1, 'series': []})))

    def test_corrupt_oversize_and_wrong_shapes_are_rejected_without_raw_content(self):
        cases = [None, [], {'schema': 2, 'series': []}, {'schema': 1, 'series': {}},
                 {'schema': 1, 'series': [None]}]
        for field, value in [('id', '../secret'), ('title', None), ('metrics', 'bad'), ('metrics', [1]),
                             ('metrics', [[1]]), ('findings', None), ('stories', None)]:
            data = small_archive(); data['series'][0][field] = value; cases.append(data)
        data = small_archive(); data['series'].append(copy.deepcopy(data['series'][0])); cases.append(data)
        data = small_archive(); del data['series'][0]['description']; cases.append(data)
        for field, value in [('id', '../private'), ('moments', []), ('moments', [{'number': 2,'session':1,'body':'x'}]),
                             ('moments', [{'number': 1,'session':0,'body':'x'}]),
                             ('moments', [{'number': 1,'session':True,'body':'x'}]),
                             ('moments', [{'number': 1,'session':1,'body':None}])]:
            data = small_archive(); data['series'][0]['stories'][0][field] = value; cases.append(data)
        data = small_archive(); data['series'][0]['stories'].append(copy.deepcopy(data['series'][0]['stories'][0])); cases.append(data)
        for number in (True, 1.0):
            data=small_archive(); data['series'][0]['stories'][0]['moments'][0]['number']=number; cases.append(data)
        for data in cases:
            with self.subTest(data=str(data)[:60]), self.assertRaisesRegex(ValueError, 'Архив результатов повреждён'):
                self.load(data)
        for raw in (b'{not-json', b' ' * (MAX_BYTES + 1), b'\xff'):
            self.path.write_bytes(raw)
            with self.assertRaises(ValueError):load_archive(self.path)

    def test_invalid_navigation_cannot_select_a_file_or_an_absent_moment(self):
        data = self.load(small_archive())
        for query in ({'series':'../../auth'}, {'story':'missing'}, {'moment':1},
                      {'story':'D001','moment':0}, {'story':'D001','moment':3},
                      {'story':'D001','moment':True}):
            with self.subTest(query=query), self.assertRaises(ValueError):render_archive(data, **query)
        first = render_archive(data, series='first', story='D001')
        last = render_archive(data, series='first', story='D001', moment=2)
        self.assertIn('Первый текст', first)
        self.assertNotIn('Последний текст', first)
        self.assertNotIn('← Предыдущий эпизод', first)
        self.assertIn('Следующий эпизод', first)
        self.assertIn('← Предыдущий эпизод', last)
        self.assertNotIn('Следующий эпизод', last)
        data['series'][0]['stories'] = []
        self.assertNotIn('Биографии по эпизодам', render_archive(data))

    def test_model_text_and_metadata_are_escaped_and_queries_encoded(self):
        hostile = '<script>alert(1)</script><img src=x onerror=alert(2)>'
        data = small_archive()
        data['series'][0].update(title=hostile, description=hostile, findings=[hostile])
        data['series'][0]['stories'][0]['moments'][0]['body'] = '**' + hostile + '**\n\n# ' + hostile + '\n\n' + hostile
        html = render_archive(self.load(data), story='D001')
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img ', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('<h3>', html)
        self.assertIn('<br>', text_blocks('одна\nдве'))
        self.assertEqual(url('a b','x/y',2), '/experiments?series=a+b&story=x%2Fy&moment=2#episode')

    def test_snapshot_builder_rejects_a_truncated_biography(self):
        builder = runpy.run_path(str(ROOT / 'scripts/experiments/build_panel_snapshot.py'))
        original = Path.read_bytes
        def read(path):
            return b'# missing moments' if path.name == 'D001.md' else original(path)
        with patch.object(Path,'read_bytes',read), self.assertRaisesRegex(ValueError,'18 моментов'):
            builder['build'](ROOT)


class ExperimentHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Runtime(Path(self.tmp.name) / 'demo.db', demo=True, interval=.01)
        self.client = self.enterContext(TestClient(create_app(self.runtime,port=18767),base_url='http://127.0.0.1:18767'))

    def test_actual_archive_biography_steps_and_selection_do_not_run_a_session(self):
        before = self.runtime.submit('view').result(5)
        response = self.client.get('/experiments')
        self.assertEqual(response.status_code,200)
        self.assertIn('216',response.text)
        self.assertNotIn('Стенд экспериментов появится',response.text)
        for key in ('matrix','motivation','agency'):
            self.assertEqual(self.client.get('/experiments',params={'series':key}).status_code,200)
        response=self.client.get('/experiments',params={'series':'development','story':'D001','moment':4})
        self.assertEqual(response.status_code,200)
        self.assertIn('Я не разделяю этот интерес',response.text)
        self.assertNotIn('Как сравнить два объяснения',response.text)
        self.assertIn('name="moment"',response.text)
        self.assertIn('Внутренняя запись',response.text)
        after=self.runtime.submit('view').result(5)
        self.assertEqual(after['version'],before['version'])
        self.assertEqual(after['session_state'],'stopped')
        self.assertEqual(after['usage']['calls'],0)
        live_header={**after,'demo':False}
        self.assertIn('Codex · GPT-6-Luna',render('',live_header,'token'))

    def test_invalid_selection_is_a_recoverable_400_not_a_file_read(self):
        for query in ({'series':'../auth.json'},{'story':'<script>bad</script>'},
                      {'series':'development','story':'D001','moment':'bad'},
                      {'series':'development','story':'D001','moment':99}):
            response=self.client.get('/experiments',params=query)
            self.assertEqual(response.status_code,400)
            self.assertIn('Не удалось открыть результаты',response.text)
            self.assertNotIn('<script>bad</script>',response.text)

    def test_missing_corrupted_or_unreadable_snapshot_has_explicit_state(self):
        path=Path(self.tmp.name)/'other.json'
        runtime=Runtime(Path(self.tmp.name)/'other.db',demo=True,interval=.01)
        with TestClient(create_app(runtime,port=18768,experiment_snapshot=path),base_url='http://127.0.0.1:18768') as client:
            response=client.get('/experiments')
            self.assertEqual(response.status_code,200)
            self.assertIn('пока не установлен',response.text)
            path.write_text('{broken')
            response=client.get('/experiments')
            self.assertEqual(response.status_code,503)
            self.assertIn('Повторить открытие результатов',response.text)
            self.assertNotIn('пока не установлен',response.text)
            with patch('iam.panel.app.load_archive',side_effect=PermissionError('SECRET-PATH')):
                response=client.get('/experiments')
            self.assertEqual(response.status_code,503)
            self.assertNotIn('SECRET-PATH',response.text)
