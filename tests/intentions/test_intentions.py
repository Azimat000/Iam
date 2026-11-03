import json

from iam.domain import Op
from iam.stubs import act, rest, Reply
from iam.world import WRITE
from world.helpers import LivingCase


class IntentionTest(LivingCase):
    def test_goal_and_action_are_independent_slots_in_both_orders(self):
        source = self.read()
        for reverse in (False,True):
            goal = self.goal(source,iid='goal_' + str(reverse))
            action = act(WRITE,{'project_id':'poem','title':'Работа','text':'Свет у окна.\nТёплая весна.'})
            report = self.run_reply(*( (action,goal) if reverse else (goal,action)))
            self.assertEqual(report.outcome,'done', report)
            self.assertEqual(report.actions[0].status,'confirmed')
            self.assertEqual(self.c.state['intentions'][goal['body']['intention_id']]['status'],'active')

    def test_five_statuses_history_and_closed_states(self):
        source = self.read()
        self.run_reply(self.goal(source))
        for status in ('waiting_opportunity','active','deferred','active','abandoned'):
            self.run_reply(self.transition(status))
            self.assertEqual(self.c.state['intentions']['my_goal']['status'],status)
        report=self.run_reply(self.transition('active'))
        self.assertEqual(report.outcome,'rejected')
        self.assertEqual(len(self.c.state['intentions']['my_goal']['history']),6)

    def test_done_requires_late_matching_result_and_two_reviews(self):
        source = self.read()
        self.run_reply(self.goal(source))
        result = self.write('Свет у окна.\nВесна пришла.')
        report = self.run_reply(self.transition('done', result=result),
                               replies=(self.assessment(),self.assessment('evaluation')))
        self.assertEqual(report.outcome,'done',report)
        goal = self.c.state['intentions']['my_goal']
        self.assertEqual(goal['status'],'done')
        self.assertTrue(goal['completion']['passed'])
        self.assertFalse(goal['completion']['semantic_verified'])
        stages = [e.payload for e in self.c.store.events() if e.type=='proposal.assessed']
        self.assertEqual(len(stages),3)
        self.assertIn('response',stages[0])
        self.assertNotIn('available_result',json.loads(stages[0]['request']['context'][1]['text']))

    def test_failed_or_wrong_project_or_early_or_unchanged_result_never_done(self):
        source = self.read()
        early = self.write('Первый текст')
        self.run_reply(self.goal(source))
        other = self.write('Другой текст','other')
        for record in (source,early,other):
            before=len(self.p.calls)
            self.run_reply(self.transition('done',result=record))
            self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')
            self.assertEqual(len(self.p.calls),before+1)
        same=self.write('Первый текст')
        self.run_reply(self.transition('done',result=same))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')

    def test_own_calculation_is_not_independent_observation(self):
        source=self.read()
        self.run_reply(self.goal(source,text='Независимо проверить прогноз температуры',criterion='Есть измеренное значение'))
        result=self.write('Расчёт даёт 20 градусов.')
        self.run_reply(self.transition('done',result=result),replies=(self.assessment(kind='independent_observation'),))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')
        self.assertEqual(self.p.calls[-1]['purpose'],'appraise')

    def test_invalid_or_failed_reviewer_keeps_goal_and_raw_diagnostics(self):
        source=self.read()
        self.run_reply(self.goal(source))
        result=self.write()
        invalid=lambda request: Reply((rest('плохая оценка',cites=['evt_Missing001']),))
        self.run_reply(self.transition('done',result=result),replies=(invalid,))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')
        stages=[e.payload for e in self.c.store.events() if e.type=='proposal.assessed']
        self.assertEqual(stages[-2]['error'],'assessment_format_or_cites')
        self.assertIsNotNone(stages[-2]['response'])
        self.run_reply(self.transition('done',result=result),replies=(self.assessment(),self.assessment('evaluation',verdict='fail')))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')

    def test_forgotten_revised_and_off_sources_do_not_return_via_goals(self):
        source=self.read()
        self.run_reply(self.goal(source,text='Скрываемый собственный замысел'))
        self.assertTrue(self.m.fade(source.memory_id).applied)
        self.run_reply(rest())
        self.assertNotIn('Скрываемый собственный замысел',json.dumps(self.p.calls[-1],ensure_ascii=False))
        self.assertFalse(self.cycle.intentions.visible())
        self.m.off=True
        self.assertFalse(self.m.decision_state()['intentions'])

    def test_criterion_cannot_change_and_direct_state_changes_are_rejected(self):
        source=self.read()
        self.run_reply(self.goal(source))
        pr=self.transition('active')
        pr['body'].update(op='update',criterion='любая запись')
        self.run_reply(pr)
        self.assertEqual(self.c.state['intentions']['my_goal']['criterion'],'Есть две строки о свете')
        result=self.c.apply(self.prop(self.c,Op('intentions','my_goal.criterion','set','готово',True)))
        self.assertFalse(result.applied)

    def test_no_goal_without_visible_live_basis_or_action(self):
        source=self.read()
        for mutate in (lambda p:p.update(cites=[]), lambda p:p['body'].update(operation='fly'),
                       lambda p:p['body'].update(project_id='bad.key'),lambda p:p['body'].update(text='')):
            pr=self.goal(source)
            mutate(pr)
            self.run_reply(pr)
            self.assertNotIn('my_goal',self.c.state['intentions'])
        self.run_reply(rest(),self.goal(source))
        self.assertNotIn('my_goal',self.c.state['intentions'])
