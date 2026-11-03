import json

from iam.domain import Op, new_id
from iam.stubs import Reply, rest, act
from iam.world import WRITE
from world.helpers import LivingCase


class MotivationTest(LivingCase):
    def test_duplicates_legacy_outcomes_and_nonworld_actions_do_not_create_extra_motivation(self):
        from iam.state import EventSpec
        from iam.interfaces import Capability
        from iam.stubs import StubWorld
        source=self.read()
        duplicate=self.m.prepare(kind='episodic',origin='observation',text=source.text,sources=source.sources,
                                 content={'experience':'live','outcome':'confirmed','action_id':source.content['action_id']})
        self.assertTrue(self.m.write(duplicate).applied)
        items=self.signals()['items']
        self.assertEqual(len(items),1)
        self.assertEqual(len(items[0]['sources']),1)
        legacy='act_LegacyHistory01'
        done=self.c.apply(self.prop(self.c,Op('world_time','world.external_history','set',True,True),actor='world',
                                   carrier=EventSpec('action.confirmed','act:'+legacy+':ok',{'action_id':legacy})))
        self.assertTrue(done.applied)
        row=self.m.prepare(kind='episodic',origin='observation',text='Внешний исход прежнего формата',
                           sources=(done.events[0].event_id,),content={'outcome':'confirmed','action_id':legacy})
        self.assertTrue(self.m.write(row).applied)
        self.assertEqual(len(self.signals()['items']),1)
        self.world.external=StubWorld()
        self.caps.register(Capability('cap_External01','Внешняя работа'))
        report=self.run_reply(act('cap_External01',{'other':'data'}))
        self.assertEqual(report.actions[0].status,'confirmed')
        self.run_reply(rest())
        self.assertEqual(len(self.signals()['items']),1)

    def test_dependent_motivation_cache_is_hidden_in_off_and_methods_need_distinct_evidence(self):
        source=self.read();self.estimate(source)
        estimate=next(r for r in self.m.records() if 'motivation' in r.content)
        child=self.m.prepare(kind='semantic',origin='own_hypothesis',text='Зависимый МОТИВ_В_КЕШЕ',sources=(estimate.memory_id,))
        self.assertTrue(self.m.write(child).applied)
        self.cycle.motivation.off=True
        self.run_reply(rest())
        self.assertNotIn('МОТИВ_В_КЕШЕ',json.dumps(self.p.calls[-1],ensure_ascii=False))
        self.cycle.motivation.off=False
        self.run_reply(self.goal(source));result=self.write()
        self.run_reply(self.transition('done',result=result),replies=(self.assessment(),self.assessment('evaluation')))
        assessment=next(r for r in self.m.records() if 'skill_assessment' in r.content)
        # Повторная запись того же результата не удваивает измерение и не обучает методу.
        other=self.m.prepare(kind='semantic',origin='model_explanation',text=assessment.text,sources=assessment.sources,
                             content={'skill_assessment':assessment.content['skill_assessment']})
        self.assertTrue(self.m.write(other).applied)
        self.assertEqual(len(self.cycle.motivation.learning.measurements()['poem']['samples']),1)
        pr={'proposal_id':new_id('prop'),'kind':'memory_write','rationale':'Метод по дубликатам',
            'cites':[assessment.memory_id,other.memory_id], 'body':{'draft':{'kind':'procedural','origin':'own_hypothesis',
                  'content':{'text':'Метод','procedure':{'skill_id':'poem','method':'Повтор'}}}}}
        self.run_reply(pr)
        self.assertFalse(self.cycle.motivation.learning.procedures())
        fake=self.m.prepare(kind='semantic',origin='model_explanation',text='Непривязанный навык',sources=(child.memory_id,),
                            content={'skill_assessment':assessment.content['skill_assessment']})
        self.assertTrue(self.m.write(fake).applied)
        self.assertNotIn(fake.memory_id,self.cycle.motivation.learning.assessments())
    def test_motivation_off_removes_goals_signals_and_cached_hypotheses_from_real_request(self):
        source=self.read();self.estimate(source)
        self.run_reply(self.goal(source,text='МОТИВАЦИОННЫЙ_ЗАМЫСЕЛ'))
        self.run_reply(rest())
        self.assertIn('МОТИВАЦИОННЫЙ_ЗАМЫСЕЛ',json.dumps(self.p.calls[-1],ensure_ascii=False))
        self.cycle.motivation.off=True
        self.run_reply(rest())
        blob=json.dumps(self.p.calls[-1],ensure_ascii=False)
        self.assertNotIn('МОТИВАЦИОННЫЙ_ЗАМЫСЕЛ',blob)
        self.assertNotIn('Хочу ещё поработать с этой темой',blob)
        self.assertIn('Снег тает',blob)
        self.assertIn('"goal_signal": 0.0',self.p.calls[-1]['context'][-3]['text'])
        self.cycle.motivation.off=False
        self.assertTrue(self.cycle.intentions.visible())

    def test_hidden_goal_cannot_be_completed_or_sent_to_reviewer_in_off(self):
        source=self.read();self.run_reply(self.goal(source,text='HIDDEN_MOTIVATION_GOAL_MARKER'));result=self.write()
        self.cycle.motivation.off=True
        before=len(self.p.calls)
        self.run_reply(self.transition('done',result=result))
        self.assertEqual(len(self.p.calls),before+1)
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')
        self.assertNotIn('HIDDEN_MOTIVATION_GOAL_MARKER',json.dumps(self.p.calls[-1],ensure_ascii=False))

    def test_revised_assessment_cannot_return_forgotten_quote_or_teach_procedure(self):
        source=self.read();self.run_reply(self.goal(source));result=self.write('OLD_FORGOTTEN_ARTIFACT_MARKER')
        self.run_reply(self.transition('done',result=result),replies=(self.assessment(),self.assessment('evaluation')))
        row=next(r for r in self.m.records() if 'skill_assessment' in r.content)
        other=self.read()
        self.assertTrue(self.m.revise(row.memory_id,'Исправление: оценка больше не относится к работе',
                                     sources=(other.memory_id,),reason='Пересмотр основания').applied)
        self.assertTrue(self.m.fade(result.memory_id).applied)
        self.assertFalse(self.cycle.motivation.learning.measurements())
        self.assertFalse(self.cycle.motivation.learning.assessments())
        self.run_reply(rest())
        self.assertNotIn('OLD_FORGOTTEN_ARTIFACT_MARKER',json.dumps(self.p.calls[-1],ensure_ascii=False))

    def test_learning_rejects_unassessed_methods_and_off_hides_skill(self):
        source=self.read()
        pr={'proposal_id':new_id('prop'),'kind':'memory_write','rationale':'Предлагаю метод','cites':[source.memory_id],
            'body':{'draft':{'kind':'procedural','origin':'own_hypothesis','content':{'text':'Способ',
                         'procedure':{'skill_id':'poem','method':'Повторять собственный текст'}}}}}
        self.run_reply(pr)
        self.assertFalse(self.cycle.motivation.learning.procedures())
        source2=self.write()
        pr['proposal_id']=new_id('prop');pr['cites']=[source.memory_id,source2.memory_id]
        self.run_reply(pr)
        self.assertFalse(self.cycle.motivation.learning.procedures())
        self.m.off=True
        self.assertEqual(self.cycle.motivation.learning.measurements(),{})
        self.assertEqual(self.cycle.motivation.learning.procedures(),{})

    def test_failed_and_uncertain_semantic_outcomes_do_not_measure_success(self):
        source=self.read();self.run_reply(self.goal(source));result=self.write()
        self.run_reply(self.transition('done',result=result),replies=(self.assessment(),self.assessment('evaluation',verdict='fail')))
        self.assertEqual(self.cycle.motivation.learning.measurements()['poem']['estimate'],0)
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')
        result=self.write('Другая попытка')
        self.run_reply(self.transition('done',result=result),replies=(self.assessment(kind='uncertain'),))
        values=self.cycle.motivation.learning.measurements()['poem']
        self.assertEqual(values['estimate'],0)
        self.assertEqual(len(values['samples']),2)

    def test_skill_uses_content_criteria_and_procedure_needs_distinct_assessed_results(self):
        source=self.read()
        for n in (1,2):
            self.run_reply(self.goal(source,iid='goal'+str(n)))
            result=self.write('Свет у окна '+str(n)+'.\nВесна у порога.')
            self.run_reply(self.transition('done',iid='goal'+str(n),result=result),
                           replies=(self.assessment(),self.assessment('evaluation')))
        learning=self.cycle.motivation.learning
        measure=learning.measurements()['poem']
        self.assertEqual(measure['estimate'],1)
        self.assertEqual(len(measure['samples']),2)
        self.assertTrue(all(s['criterion']=='Есть две строки о свете' for s in measure['samples']))
        rows=[r for r in self.m.records() if 'skill_assessment' in r.content]
        pr={'proposal_id':new_id('prop'),'kind':'memory_write','rationale':'Нашла способ писать',
            'cites':[r.memory_id for r in rows], 'body':{'draft':{'kind':'procedural','origin':'own_hypothesis',
            'content':{'text':'Писать две строки по образу из прочитанного',
                       'procedure':{'skill_id':'poem','method':'Вначале образ, затем связанная с ним строка'}}}}}
        self.run_reply(pr)
        self.assertIn('poem',learning.procedures())
        self.assertIn('Вначале образ',learning.procedures()['poem']['method'])
        self.c.pause_session('operator')
        self.c=self.reopen(self.c)
        from iam.world import PersistentWorld
        from iam.world.cycle import LivingCycle,WorldMemory
        from iam.stubs import StubCreatorBridge
        self.world=PersistentWorld(self.c)
        self.m=WorldMemory(self.c)
        self.cycle=LivingCycle(self.c,self.p,self.m,self.world,self.caps,StubCreatorBridge())
        self.assertIn('poem',self.cycle.motivation.learning.procedures())
        self.c.resume_session()
        self.m.fade(rows[0].memory_id)
        self.assertFalse(self.cycle.motivation.learning.procedures())

    def estimate(self, source, topic='poem',interest=.8,need=.4):
        pr={'proposal_id':new_id('prop'),'kind':'memory_write','rationale':'Осмыслила занятие','cites':[source.memory_id],
            'body':{'draft':{'kind':'semantic','origin':'own_hypothesis','content':{'text':'Хочу ещё поработать с этой темой',
                    'motivation':{'topic':topic,'interest':interest,'need':need}}}}}
        self.run_reply(pr)
        return pr

    def signals(self):
        return self.cycle.motivation.signals(self.m.records())

    def test_interest_from_experience_and_no_reward_for_writing(self):
        source=self.read()
        self.estimate(source)
        signals=self.signals()
        self.assertEqual(signals['interests']['poem']['interest'],.8)
        self.assertEqual(signals['needs']['poem'],.4)
        self.assertFalse(signals['selects_action'])
        self.write('Свет и весна')
        self.write('Другая новая строка')
        signals=self.signals()
        item=next(v for v in signals['items'] if v['target']=='poem')
        self.assertEqual(item['interest'],.8)
        self.assertIsNone(item['meaningful_progress'])
        self.assertIsNone(item['skill'])

    def test_repeat_reduces_estimate_but_idle_does_not(self):
        source=self.read()
        self.estimate(source)
        self.write('Тот же текст')
        baseline=self.signals()
        self.time.advance(10000)
        self.assertEqual(self.signals(),baseline)
        self.write('  Тот   же текст  ')
        item=next(v for v in self.signals()['items'] if v['target']=='poem')
        self.assertEqual(item['exact_repeats'],1)
        self.assertAlmostEqual(item['interest'],.65)
        self.assertEqual(item['text_novelty'],0)

    def test_failed_unknown_feedback_and_off(self):
        from iam.interfaces import ActionOutcome
        source=self.read()
        self.estimate(source)
        original=self.world.execute
        for outcome in ('failed','unknown'):
            self.world.execute=lambda request:ActionOutcome(outcome,'Не подтверждён результат')
            report=self.run_reply(act(WRITE,{'project_id':'poem','title':'Работа','text':'Проба'}))
            self.assertIn(report.actions[0].status,('failed','unknown_outcome'))
            item=next(v for v in self.signals()['items'] if v['target']=='poem')
            self.assertEqual(item['consecutive_failures'],1 if outcome=='failed' else 0)
            self.assertAlmostEqual(item['interest'],.55 if outcome=='failed' else .8)
        self.world.execute=original
        self.cycle.motivation.off=True
        self.assertEqual(self.signals(),{'enabled':False})
        self.cycle.motivation.off=False
        self.m.off=True
        self.assertEqual(self.signals(),{'enabled':False})

    def test_forgotten_and_revised_estimate_is_not_used(self):
        source=self.read()
        self.estimate(source)
        self.assertTrue(self.signals()['interests'])
        self.assertTrue(self.m.fade(source.memory_id).applied)
        self.assertFalse(self.signals()['interests'])
        self.run_reply(rest())
        self.assertNotIn('Хочу ещё поработать',json.dumps(self.p.calls[-1],ensure_ascii=False))

    def test_invalid_hypotheses_and_direct_cache(self):
        source=self.read()
        for interest in (-1,2,True,'1',None,float('nan')):
            # NaN не входит даже в конверт JSON, проверяем разборщик напрямую.
            if isinstance(interest,float) and interest!=interest:
                continue
            self.estimate(source,interest=interest)
            self.assertFalse(self.c.state['preferences'].get('s22'))
        self.m.off=True
        self.estimate(source)
        self.assertFalse(self.c.state['preferences'].get('s22'))
        self.m.off=False
        result=self.c.apply(self.prop(self.c,Op('preferences','s22.poem','set',{'interest':1},True)))
        self.assertFalse(result.applied)

    def test_six_scenario_mechanisms_without_forcing_free_behavior(self):
        source=self.read() # возникновение интереса
        self.estimate(source)
        self.run_reply(self.goal(source))
        self.run_reply(self.transition('deferred'))
        self.run_reply(self.transition('active')) # возврат
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')
        self.write('Один текст')
        self.write('Один текст') # повтор без продвижения
        self.assertEqual(next(v for v in self.signals()['items'] if v['target']=='poem')['exact_repeats'],1)
        self.write('Другая тема','other') # переключение
        self.assertEqual(len(self.world.state['projects']),2)
        self.run_reply(rest()) # допустимое бездействие
        self.assertEqual(self.c.state['affect']['attention']['focus'],'rest')
