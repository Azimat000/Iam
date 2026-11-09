import json

from iam.domain import Op
from iam.stubs import act, rest, Reply
from iam.world import WRITE
from world.helpers import LivingCase


class IntentionTest(LivingCase):
    def restart_living_cycle(self):
        from iam.world import PersistentWorld
        from iam.world.cycle import LivingCycle, WorldMemory
        from iam.stubs import StubCreatorBridge
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        self.world = PersistentWorld(self.c)
        self.m = WorldMemory(self.c)
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.c.resume_session()

    def test_repeated_post_goal_work_after_restart_does_not_start_done_reviews(self):
        source = self.read()
        self.run_reply(self.goal(source))
        first = self.write('Свет у окна.\nВесна пришла.')
        self.restart_living_cycle()
        repeated = self.write('Свет у окна.\nВесна пришла.')
        self.assertNotEqual(first.memory_id, repeated.memory_id)
        before = len(self.p.calls)
        self.run_reply(self.transition('done', result=repeated))
        self.assertEqual(len(self.p.calls), before + 1)
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'active')
        self.assertFalse([e for e in self.c.store.events() if e.type == 'proposal.assessed'])
        self.assertNotIn('previous_fingerprint', json.dumps(self.p.calls[-1]))

    def test_previous_fingerprint_never_reveals_forgotten_work_in_context(self):
        from iam.world import fingerprint
        source = self.read()
        previous = self.write('PRIVATE_OLD_TEXT_5729')
        current = self.write('Новая доступная работа')
        self.assertTrue(self.m.fade(previous.memory_id).applied)
        self.run_reply(rest())
        request = json.dumps(self.p.calls[-1], ensure_ascii=False)
        self.assertIn(current.memory_id, request)
        self.assertNotIn('PRIVATE_OLD_TEXT_5729', request)
        self.assertNotIn(fingerprint('PRIVATE_OLD_TEXT_5729'), request)
        self.assertNotIn('previous_fingerprint', request)

    def test_original_valid_work_can_finish_after_restart_without_claiming_late_progress(self):
        source = self.read()
        self.run_reply(self.goal(source))
        result = self.write('Свет у окна.\nВесна пришла.')
        receipt = self.world.state['actions'][result.content['action_id']]['receipt']
        self.assertIn('previous_fingerprint', receipt)
        self.assertIsNone(receipt['previous_fingerprint'])
        self.restart_living_cycle()
        self.run_reply(self.transition('done', result=result),
                       replies=(self.assessment(), self.assessment('evaluation')))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'done')

    def test_legacy_receipt_remains_compatible_without_claiming_known_previous_text(self):
        from unittest.mock import patch
        source = self.read()
        self.run_reply(self.goal(source))
        original = self.world.result_ops

        def legacy(*args):
            ops = original(*args)
            for op in ops:
                if op.path.endswith('.receipt'):
                    op.value.pop('previous_fingerprint', None)
            return ops

        with patch.object(self.world, 'result_ops', side_effect=legacy):
            result = self.write('Свет у окна.\nВесна пришла.')
        self.restart_living_cycle()
        receipt = self.world.state['actions'][result.content['action_id']]['receipt']
        self.assertNotIn('previous_fingerprint', receipt)
        self.run_reply(self.transition('done', result=result),
                       replies=(self.assessment(), self.assessment('evaluation')))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'done')

    def test_same_text_in_another_project_does_not_make_first_work_a_repeat(self):
        source = self.read()
        self.write('Свет у окна.\nВесна пришла.', project='other')
        self.run_reply(self.goal(source))
        result = self.write('Свет у окна.\nВесна пришла.')
        self.assertIsNone(self.world.state['actions'][result.content['action_id']]['receipt']['previous_fingerprint'])
        self.run_reply(self.transition('done', result=result),
                       replies=(self.assessment(), self.assessment('evaluation')))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'done')

    def test_whitespace_repeat_after_restart_does_not_start_done_reviews(self):
        source = self.read()
        self.run_reply(self.goal(source))
        self.write('Свет у окна.')
        self.restart_living_cycle()
        repeated = self.write('  Свет   у окна.\n')
        before = len(self.p.calls)
        self.run_reply(self.transition('done', result=repeated))
        self.assertEqual(len(self.p.calls), before + 1)
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'active')

    def test_changed_work_after_restart_is_eligible(self):
        source = self.read()
        self.run_reply(self.goal(source))
        self.write('Свет у окна.')
        self.restart_living_cycle()
        changed = self.write('Свет у окна.\nВесна пришла.')
        self.run_reply(self.transition('done', result=changed),
                       replies=(self.assessment(), self.assessment('evaluation')))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'done')

    def test_future_moment_invalid_values_and_recovery_do_not_reset_or_duplicate(self):
        source=self.read()
        for value in (-1,True,'1000'):
            pr=self.goal(source);pr['body']['not_before_life_ms']=value
            self.run_reply(pr)
            self.assertFalse(self.c.state['intentions'])
        pr=self.goal(source);pr['body']['not_before_life_ms']=1000
        self.run_reply(pr)
        mid=self.c.state['intentions']['my_goal']['moment_id']
        self.assertTrue(self.c.cancel_moment(mid).applied)
        self.cycle._schedule_intentions()
        self.assertIn(mid,self.c.state['world_time']['moments'])
        self.time.advance(1000)
        self.c.fire_due_moments()
        self.cycle._schedule_intentions()
        self.assertNotIn(mid,self.c.state['world_time']['moments'])

    def test_knowing_wanting_and_permission_are_separate(self):
        source=self.read();self.run_reply(self.goal(source))
        self.caps.set_status(WRITE,'revoked')
        report=self.run_reply(act(WRITE,{'project_id':'poem','title':'Работа','text':'Свет'}))
        self.assertFalse(report.actions)
        goal=self.c.state['intentions']['my_goal']
        self.assertTrue(goal['knows']);self.assertTrue(goal['wants'])
        self.run_reply(self.transition('abandoned'))
        self.assertFalse(self.c.state['intentions']['my_goal']['wants'])
        self.caps.set_status(WRITE,'available')
        self.write('Закрываемая работа','closed')
        self.run_reply(act(WRITE,{'project_id':'closed','to_status':'abandoned'}))
        self.run_reply(self.goal(source,iid='closed_goal',project='closed'))
        self.assertNotIn('closed_goal',self.c.state['intentions'])

    def test_future_intention_survives_pause_and_fires_once_in_own_time(self):
        source=self.read()
        pr=self.goal(source);pr['body']['not_before_life_ms']=1000
        self.run_reply(pr)
        goal=self.c.state['intentions']['my_goal'];mid=goal['moment_id']
        self.assertIn(mid,self.c.state['world_time']['moments'])
        result=self.write()
        before=len(self.p.calls)
        self.run_reply(self.transition('done',result=result))
        self.assertEqual(len(self.p.calls),before+1)
        self.c.pause_session('operator');self.time.advance(5000)
        self.assertEqual(self.c.life_time_ms,0)
        self.cycle._schedule_intentions()
        self.c=self.reopen(self.c)
        self.assertIn(mid,self.c.state['world_time']['moments'])
        self.c.resume_session();self.time.advance(1000)
        events=self.c.fire_due_moments()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0].events[0].payload['owner_ref'],'my_goal')
        self.assertFalse(self.c.fire_due_moments())

    def test_long_material_is_fully_visible_before_complete_read_is_accepted(self):
        self.c.pause_session('operator')
        value='Первый абзац. '*550+'КОНЕЦ_НУЖНО_УВИДЕТЬ'
        long_id=self.world.add_material('Длинный текст',value,'Проверить полное чтение')
        self.c.resume_session()
        source=self.read()
        self.run_reply(self.goal(source,project=long_id,text='Прочитать длинный текст полностью',criterion='Весь текст до конца',operation='read'))
        from iam.world import READ
        report=self.run_reply(act(READ,{'material_id':long_id}))
        result=self.c.store.get_memory(report.actions[0].memory_id)
        self.run_reply(self.transition('done',result=result), replies=(self.assessment(kind='received_text'),self.assessment('evaluation')))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'done')
        packet=json.loads(self.p.calls[-1]['context'][0]['text'])['packet']
        self.assertEqual(packet['available_result']['text'],value)
        self.assertIn('КОНЕЦ_НУЖНО_УВИДЕТЬ',packet['available_result']['text'])

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

    def test_inactive_goal_does_not_start_completion_reviews(self):
        source=self.read();self.run_reply(self.goal(source));result=self.write()
        self.run_reply(self.transition('deferred'))
        before=len(self.p.calls)
        self.run_reply(self.transition('done',result=result))
        self.assertEqual(len(self.p.calls),before+1)
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'deferred')

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
        self.assertNotIn('available_result',json.loads(stages[0]['request']['context'][0]['text'])['packet'])
        self.assertEqual(len(stages[0]['request']['context']),1)
        self.assertEqual(stages[0]['protocol'],'single_packet_v2')

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

    def test_unavailable_target_origin_duplicate_and_transition_extra_fields(self):
        source=self.read()
        for operation,target in (('read','missing'),('move','missing')):
            self.run_reply(self.goal(source,operation=operation,project=target))
            self.assertFalse(self.c.state['intentions'])
        prepared=self.m.prepare(kind='episodic',origin='own_hypothesis',text='Не наблюдение',sources=(source.memory_id,))
        self.m.write(prepared)
        self.run_reply(self.goal(prepared))
        self.assertFalse(self.c.state['intentions'])
        self.run_reply(self.goal(source))
        self.run_reply(self.goal(source))
        self.assertEqual(len(self.c.state['intentions']),1)
        pr=self.transition('deferred'); pr['body']['result_ref']=source.memory_id
        self.run_reply(pr)
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')
        self.cycle.intentions.grants.clear()
        direct=self.c.apply(self.prop(self.c,Op('intentions','my_goal.history','set',[],True)))
        self.assertFalse(direct.applied)
        bad=self.goal(source);bad['body']['intention_id']='bad.key'
        self.run_reply(bad)
        self.assertNotIn('bad.key',self.c.state['intentions'])
        incomplete=self.goal(source);incomplete['body']={'op':'create','intention_id':'incomplete'}
        self.run_reply(incomplete)
        self.assertNotIn('incomplete',self.c.state['intentions'])

    def test_revised_result_and_incomplete_display_do_not_reveal_original(self):
        from iam.cycle.attention import Focus
        source=self.read(); self.run_reply(self.goal(source)); result=self.write('Исходный секретный результат')
        pr=self.transition('done',result=result)
        trigger=self.due()
        context=self.cycle._context(trigger,Focus('activity',None),self.m.records())
        request=self.cycle._request('cyc_TestResult01',context,0,None)
        _,shown=self.cycle.intentions.result(pr,request)
        self.assertEqual(shown['text'],'Исходный секретный результат')
        request['context']=[i for i in context if 'world_results' not in i['text']]
        with self.assertRaises(ValueError):self.cycle.intentions.result(pr,request)
        request['context']=context+[{'ref':trigger.event_id,'origin':'observation','layer':'state','text':'[]'}]
        self.cycle.intentions.result(pr,request)
        pr['cites']=[]
        with self.assertRaises(ValueError):self.cycle.intentions.result(pr,request)
        self.m.revise(result.memory_id,'Исправленный текст',sources=result.sources,reason='Исправить источник')
        self.run_reply(rest())
        self.assertNotIn('Исходный секретный результат',json.dumps(self.p.calls[-1],ensure_ascii=False))
        self.run_reply(self.transition('done',result=self.m._source(result.memory_id)))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')
