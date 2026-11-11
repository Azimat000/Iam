import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from iam.panel.app import create_app
from iam.panel.runtime import Runtime


class WorldHTTPTest(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime=Runtime(Path(self.directory.name)/'demo.db',demo=True,interval=100)
        self.client=TestClient(create_app(self.runtime,port=18765),base_url='http://127.0.0.1:18765')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)

    def fields(self):
        page=self.client.get('/world')
        self.assertEqual(page.status_code,200)
        return {name:re.search('name="'+name+'" value="([^"]*)"',page.text).group(1) for name in ('csrf','epoch','version')}

    def post(self,**kw):
        return self.client.post('/actions/material_add',data={**self.fields(),'title':'Мой текст','content':'Короткая история',
                                                            'reason':'Добавить материал',**kw},headers={'Origin':'http://127.0.0.1:18765'})

    def test_empty_upload_long_cyrillic_and_safe_html(self):
        page=self.client.get('/world')
        self.assertIn('В библиотеке пока нет текстов',page.text)
        self.assertIn('Работ пока нет',page.text)
        self.assertIn('Намерений пока нет',page.text)
        self.assertIn('Рабочий стол',page.text)
        result=self.post(title='<script>Название</script>',content='Я'*8000)
        self.assertEqual(result.status_code,200,result.text)
        self.assertIn('Текст добавлен',result.text)
        self.assertIn('&lt;script&gt;',result.text)
        self.assertNotIn('<script>',result.text)
        self.assertIn('Ещё не прочитано',result.text)
        data=self.runtime.submit('view').result(5)
        self.assertEqual(len(data['state']['world_time']['world']['library']),1)
        self.assertTrue(any(c['capability_id']=='cap_Project01' for c in data['capabilities']))

    def test_validation_preserves_fields_and_versions_csrf_and_size(self):
        result=self.post(title='Сохрани название',content='Сохрани <текст>',reason='')
        self.assertEqual(result.status_code,409)
        self.assertIn('Сохрани название',result.text)
        self.assertIn('Сохрани &lt;текст&gt;',result.text)
        self.assertEqual(self.post(csrf='wrong').status_code,403)
        self.assertEqual(self.post(version='0').status_code,409)
        self.assertEqual(self.post(content='Я'*8001).status_code,409)
        self.assertEqual(self.post(content='Я'*16000).status_code,409)
        self.assertFalse(self.runtime.submit('view').result(5)['state']['world_time']['world'].get('library'))

    def test_running_and_inflight_upload_is_not_allowed(self):
        fields=self.fields()
        def run():
            self.runtime.ctl.start_session()
            # Фиксируем обычный служебный момент до снятия версии формы: отказ должен
            # проверять запрет загрузки на ходу, а не случайно устаревшую версию.
            self.runtime.runner.reconcile()
            return self.runtime.ctl.state_version
        self.runtime._test_run=run
        version=self.runtime.submit('_test_run').result(5)
        page=self.client.get('/world')
        self.assertNotIn('/actions/material_add',page.text)
        result=self.client.post('/actions/material_add',data={**fields,'version':version,'title':'Заголовок','content':'Текст','reason':'Проверка'},headers={'Origin':'http://127.0.0.1:18765'})
        self.assertEqual(result.status_code,409)
        self.assertIn('Поставьте сеанс на паузу и дождитесь ответа модели перед загрузкой текста.', result.text)
        def flight():
            self.runtime.ctl.pause_session('operator')
            self.runtime.session.in_flight=True
            return self.runtime.ctl.state_version
        self.runtime._test_flight=flight
        version = self.runtime.submit('_test_flight').result(5)
        self.assertNotIn('/actions/material_add',self.client.get('/world').text)
        result = self.client.post('/actions/material_add', data={**fields, 'version': version,
            'title': 'Заголовок', 'content': 'Текст', 'reason': 'Проверка'},
            headers={'Origin': 'http://127.0.0.1:18765'})
        self.assertEqual(result.status_code, 409)
        self.assertIn('Поставьте сеанс на паузу и дождитесь ответа модели перед загрузкой текста.', result.text)


class WorldPageRenderTest(unittest.TestCase):
    def test_populated_world_and_legacy_missing_fields_are_safely_visible(self):
        from iam.panel.world import render_world
        from iam.panel.views import form,dump
        work='act_RenderWork01'
        data={'state':{'world_time':{'world':{
            'space':{'places':{},'objects':{}},
            'library':{'book':{'title':'<script>Название</script>','read':False,'text':'<img src=x>'}},
            'projects':{'poem':{'title':'Своя работа','latest_work':work,'status':'done'},'old':{'title':'Старый проект','latest_work':'none'}},
            'actions':{work:{'params':{'project_id':'poem'}}},
            'works':{work:{'text':'Строка света.\nВесенний вечер.','event_id':'evt_ResultView01'}}}},
            'intentions':{'goal':{'text':'Завершить две строки','status':'done','criterion':'Описать свет','knows':True,'wants':False},
                          'legacy':{'status':'deferred'}}},'session_state':'paused','usage':{'in_flight':False},
            'sending':False,'epoch':1,'version':5}
        html=render_world(data,'csrf',form,dump,values={'title':'Длинное название','content':'<текст>'})
        for visible in ('В пространстве пока нет предметов','Старый проект','Своя работа','Строка света','Выполнено',
                        'Отложено','Не задан в старом формате','не указано','&lt;script&gt;','&lt;img','&lt;текст&gt;'):
            self.assertIn(visible,html)
        self.assertNotIn('<script>',html)
        self.assertNotIn('<img',html)
