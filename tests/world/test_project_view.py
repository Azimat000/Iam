"""Редактируемый текст не выдаётся за уже выбранный замысел; фильтры прежние."""
import copy
import json
from iam.stubs import rest, act
from iam.world import WRITE
from iam.state import verify
from world.helpers import LivingCase


class ProjectViewTest(LivingCase):
    def catalog(self):
        self.assertEqual(self.run_reply(rest()).outcome, 'done')
        return next(json.loads(item['text']) for item in self.p.calls[-1]['context']
                    if item['text'].startswith('{"library"'))

    def test_note_without_goal_is_editable_but_not_an_active_commitment(self):
        self.write()
        catalog = self.catalog()
        work = catalog['projects']['poem']
        self.assertEqual(work['can_edit'], True)
        self.assertNotIn('status', work)
        self.assertEqual(catalog['intentions'], {})
        self.assertEqual(self.world.state['projects']['poem']['status'], 'active')
        self.assertEqual(self.cycle._project_view({'title':'Старый снимок','latest_work':'work_old'}),
                         {'title':'Старый снимок','latest_work':'work_old','can_edit':True})

    def test_real_active_and_abandoned_intention_stay_separate_from_editability(self):
        source = self.read(); self.write()
        self.run_reply(self.goal(source))
        catalog = self.catalog()
        self.assertEqual(catalog['intentions']['my_goal']['status'], 'active')
        self.assertTrue(catalog['projects']['poem']['can_edit'])
        self.run_reply(self.transition('abandoned'))
        catalog = self.catalog()
        self.assertEqual(catalog['intentions']['my_goal']['status'], 'abandoned')
        self.assertTrue(catalog['projects']['poem']['can_edit'])

    def test_closed_project_is_not_editable_and_storage_rules_are_unchanged(self):
        self.write()
        self.assertEqual(self.run_reply(act(WRITE, {'project_id':'poem','to_status':'abandoned'})).outcome,'done')
        catalog = self.catalog()
        self.assertFalse(catalog['projects']['poem']['can_edit'])
        self.assertEqual(catalog['projects']['poem']['closed_as'], 'abandoned')
        report = self.run_reply(act(WRITE, {'project_id':'poem','title':'Запрещено','text':'Закрытый текст'}))
        self.assertFalse(report.actions)

    def test_memory_off_and_forgotten_work_do_not_leak_through_the_new_view(self):
        source = self.write('HIDDEN_WORK_TEXT')
        self.m.off = True
        self.assertEqual(self.catalog()['projects'], {})
        self.assertNotIn('HIDDEN_WORK_TEXT', json.dumps(self.p.calls[-1]['context']))
        self.m.off = False
        self.assertTrue(self.m.fade(source.memory_id).applied)
        self.assertEqual(self.catalog()['projects'], {})
        self.assertNotIn('HIDDEN_WORK_TEXT', json.dumps(self.p.calls[-1]['context']))

    def test_goal_view_preserves_choices_criteria_and_completion_without_audit_payload(self):
        source = self.read()
        first = self.goal(source)
        first['body']['not_before_life_ms'] = self.c.life_time_ms
        self.run_reply(first)
        self.run_reply(self.goal(source, iid='other_goal', criterion='Другая исходная задача'))
        before = copy.deepcopy(self.c.state['intentions'])
        shown = self.catalog()['intentions']
        self.assertEqual(set(shown), set(before))
        for key, goal in shown.items():
            for field in ('text', 'criterion', 'project_id', 'operation', 'status', 'sources', 'created_life_ms'):
                self.assertEqual(goal[field], before[key][field])
            for field in ('history', 'source_versions', 'initial_fingerprint', 'knows', 'wants', 'created_seq'):
                self.assertNotIn(field, goal)
        self.assertEqual(shown['my_goal']['not_before_life_ms'], before['my_goal']['not_before_life_ms'])
        self.assertNotIn('not_before_life_ms', shown['other_goal'])
        self.assertEqual(self.c.state['intentions'], before)
        result = self.write('Свет у окна.\nСвет на столе.')
        report = self.run_reply(self.transition('done', result=result), replies=(
            self.assessment(), self.assessment('evaluation')))
        self.assertEqual(report.outcome, 'done')
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'done')
        self.assertEqual(self.c.state['intentions']['other_goal'], before['other_goal'])
        self.assertEqual(verify(self.c.store), [])

    def test_goal_view_never_restores_forgotten_or_disabled_sources(self):
        source = self.read()
        self.run_reply(self.goal(source))
        self.m.off = True
        self.assertEqual(self.catalog()['intentions'], {})
        self.m.off = False
        self.assertTrue(self.m.fade(source.memory_id).applied)
        self.assertEqual(self.catalog()['intentions'], {})
