import json

from iam.domain import Op
from iam.interfaces import ActionOutcome, ActionRequest
from iam.stubs import act, rest, Reply, change
from iam.world import READ, WRITE, MOVE, PersistentWorld, identifier, text
from iam.world.cycle import LivingCycle, WorldMemory
from .helpers import LivingCase


class WorldTest(LivingCase):
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
