import json

from iam.domain import Op, new_id
from iam.stubs import Reply, rest, act
from iam.world import WRITE
from world.helpers import LivingCase


class MotivationTest(LivingCase):
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
