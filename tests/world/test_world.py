import json

from iam.domain import Op
from iam.interfaces import ActionOutcome, ActionRequest
from iam.stubs import act, rest, Reply, change
from iam.world import READ, WRITE, MOVE, PersistentWorld, identifier, text
from iam.world.cycle import LivingCycle, WorldMemory
from .helpers import LivingCase


class WorldTest(LivingCase):
    def test_crash_and_pause_mid_action_leave_no_half_read(self):
        original=self.world.execute
        def crash(request):
            original(request)
            raise RuntimeError('Обрыв после подготовки')
        self.world.execute=crash
        report=self.run_reply(act(READ,{'material_id':self.material}))
        self.assertEqual(report.actions[0].status,'unknown_outcome')
        self.assertFalse(self.world.state['library'][self.material]['read'])
        def pause(request):
            outcome=original(request)
            self.c.pause_session('operator')
            return outcome
        self.world.execute=pause
        report=self.run_reply(act(READ,{'material_id':self.material}))
        self.assertEqual(report.actions[0].status,'attempted')
        self.assertFalse(self.world.state['library'][self.material]['read'])
        self.c.resume_session()
        self.cycle.settle_unfinished()
        self.assertFalse(self.world.state['library'][self.material]['read'])

    def test_real_duration_and_external_delivery_and_failed_initialization(self):
        from iam.stubs import StubWorld
        self.world.external=StubWorld()
        outcome=self.world.execute(ActionRequest('act_External01','action','cap_External01'))
        self.assertEqual(outcome.status,'confirmed')
        original=self.world.execute
        def elapsed(request):
            value=original(request)
            self.time.advance(350)
            return value
        self.world.execute=elapsed
        result=self.read()
        self.assertEqual(self.world.state['actions'][result.content['action_id']]['receipt']['duration_ms'],350)
        self.assertEqual(self.world.view(),self.world.state)
        # Отказ привратника не скрывается при первичной подготовке пустого пространства.
        from unittest.mock import patch
        from types import SimpleNamespace
        with patch.object(PersistentWorld,'state',new_callable=__import__('unittest').mock.PropertyMock,return_value={}):
            with patch.object(self.c,'intervene',return_value=SimpleNamespace(applied=False)):
                with self.assertRaises(ValueError):self.world.initialize()
        self.c.pause_session('operator')
        with patch.object(self.c,'intervene',return_value=SimpleNamespace(applied=False)):
            with self.assertRaises(ValueError):self.world.add_material('Название','Текст','Причина')

    def test_rest_action_message_and_memory_limits(self):
        from iam.stubs import say
        report=self.run_reply(rest(),act(WRITE,{'project_id':'poem','title':'Работа','text':'Свет'}))
        self.assertFalse(report.actions)
        declined=rest()
        report=self.run_reply(act(WRITE,{'project_id':'poem','title':'Работа','text':'Свет'}),declined)
        self.assertEqual(report.actions[0].status,'confirmed')
        self.assertTrue(any(e.type=='proposal.rejected' and e.payload.get('proposal_id')==declined['proposal_id']
                            for e in self.c.store.events()))
        report=self.run_reply(rest(),say(WRITE,'someone','Текст'))
        self.assertFalse(report.actions)
        source=self.read()
        def draft():
            from iam.domain import new_id
            return {'proposal_id':new_id('prop'),'kind':'memory_write','rationale':'Осмысление','cites':[source.memory_id],
                    'body':{'draft':{'kind':'semantic','origin':'own_hypothesis','content':{'text':'Мысль'}}}}
        before=len(self.m.records())
        self.run_reply(draft(),draft(),draft())
        self.assertEqual(len(self.m.records())-before,2)

    def test_project_lifecycle_requires_semantic_done_and_closed_projects_are_immutable(self):
        source=self.read()
        self.run_reply(self.goal(source))
        result=self.write('Две строки света.\nНа стол пришла весна.')
        report=self.run_reply(act(WRITE,{'project_id':'poem','to_status':'done'}))
        self.assertFalse(report.actions)
        self.run_reply(self.transition('done',result=result),replies=(self.assessment(),self.assessment('evaluation')))
        self.run_reply(act(WRITE,{'project_id':'poem','to_status':'done'}))
        self.assertEqual(self.world.state['projects']['poem']['status'],'done')
        report=self.run_reply(act(WRITE,{'project_id':'poem','title':'Новая версия','text':'Нельзя'}))
        self.assertFalse(report.actions)
        self.write('Отложенный текст','other')
        self.run_reply(act(WRITE,{'project_id':'other','to_status':'abandoned'}))
        self.assertEqual(self.world.state['projects']['other']['status'],'abandoned')
        self.assertEqual(len(self.world.state['space']['places']),3)

    def test_new_project_version_cannot_finish_using_old_goal_result(self):
        source=self.read();self.run_reply(self.goal(source));result=self.write('Свет у окна.\nВесна.')
        self.run_reply(self.transition('done',result=result),replies=(self.assessment(),self.assessment('evaluation')))
        self.write('ПРОЕКТ ПУСТ И НЕ ГОТОВ')
        report=self.run_reply(act(WRITE,{'project_id':'poem','to_status':'done'}))
        self.assertFalse(report.actions)
        self.assertEqual(self.world.state['projects']['poem']['status'],'active')

    def test_real_read_write_move_and_restart(self):
        source = self.read()
        self.assertIn('Снег тает', source.text)
        self.assertTrue(self.world.state['library'][self.material]['read'])
        record = self.write()
        aid = record.content['action_id']
        self.assertEqual(self.world.state['works'][aid]['text'], 'Свет у окна напоминает о весне.')
        receipt = self.world.state['actions'][aid]['receipt']
        self.assertEqual(receipt['duration_ms'], 0)
        moved = self.run_reply(act(MOVE, {'object_id':'notebook','place_id':'shelf'}))
        self.assertEqual(moved.actions[0].status, 'confirmed')
        self.assertEqual(self.world.state['space']['objects']['notebook']['place_id'], 'shelf')
        state = self.c.state
        self.c.pause_session('operator')
        expected = self.c.state
        self.time.advance(50000)
        self.c = self.reopen(self.c)
        self.assertEqual(self.c.state, expected)
        from iam.state import verify
        self.assertEqual(verify(self.c.store), [])
        self.assertEqual(self.c.life_time_ms, state['world_time']['life_time_ms'])

    def test_prepared_or_imagined_result_does_not_change_world(self):
        before = self.world.state
        prepared = self.world.execute(ActionRequest('act_Prepared01','action',READ,{'material_id':self.material}))
        self.assertEqual(prepared.status, 'confirmed')
        self.assertEqual(self.world.state, before)
        self.assertEqual(self.world.result_ops('act_Prepared01',ActionOutcome('unknown'),'evt_Confirm001'), ())
        self.assertFalse(self.world.state['library'][self.material]['read'])
        proposal = act(WRITE, {'project_id':'poem','title':'текст','text':'готово'}, result='уже сделано')
        report = self.run_reply(proposal)
        self.assertNotEqual(report.outcome, 'done')
        self.assertEqual(self.world.state, before)

    def test_bad_preconditions_and_direct_model_world_changes(self):
        for cap, params in ((READ,{'material_id':'missing'}), (WRITE,{'project_id':'bad.key','title':'текст','text':'x'}),
                            (MOVE,{'object_id':'notebook','place_id':'nowhere'})):
            report = self.run_reply(act(cap,params))
            self.assertFalse(report.actions)
        for path in ('world.library', 'world.works', 'world.projects', 'world.space', 'world'):
            r = self.c.apply(self.prop(self.c, Op('world_time',path,'set',{},True), actor='model'))
            self.assertFalse(r.applied, path)

    def test_unread_material_is_not_in_context(self):
        self.run_reply(rest())
        request = self.p.calls[-1]
        blob = json.dumps(request,ensure_ascii=False)
        self.assertNotIn('Снег тает', blob)
        self.assertIn('Короткая история', blob)
        self.read()
        self.run_reply(rest())
        self.assertIn('Снег тает', json.dumps(self.p.calls[-1],ensure_ascii=False))

    def test_world_input_validation_and_missing_external(self):
        for value in (None,2,{},'bad.key',''):
            self.assertFalse(identifier(value))
        self.assertFalse(text(' '))
        self.assertFalse(text(7))
        for title,content,reason in (('', 'text','reason'),('a','', 'reason'),('a','text','')):
            with self.assertRaises(ValueError):
                self.world.add_material(title,content,reason)
        for cap,params,kind in ((READ,{},'action'),(WRITE,{},'action'),(MOVE,{},'action'),('cap_Unknown01',{},'action'),(READ,{},'message')):
            with self.assertRaises(ValueError):
                self.world.check(ActionRequest('act_Invalid001',kind,cap,params))
        self.assertEqual(self.world.execute(ActionRequest('act_Missing001','action','cap_Unknown01')).status,'failed')
        self.assertEqual(self.world.execute(ActionRequest('act_Missing002','action',READ,{})).status,'failed')
        self.assertEqual(self.world.result_ops('missing',ActionOutcome('confirmed'),'evt_Confirm001'),())
        self.world.initialize()
