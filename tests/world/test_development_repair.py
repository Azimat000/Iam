import json
from iam.stubs import rest
from world.helpers import LivingCase


class GoalReviewTest(LivingCase):
    def catalog(self):
        self.run_reply(rest())
        return next(json.loads(item['text']) for item in self.p.calls[-1]['context']
                    if item['text'].startswith('{"library"'))

    def test_review_pairs_are_candidates_not_success(self):
        source = self.read()
        self.run_reply(self.goal(source))
        result = self.write('Свет у окна.\nСвет у двери.')
        pairs = self.catalog()['intention_results']['my_goal']
        self.assertEqual(pairs, [{'memory_id': result.memory_id, 'event_id': result.sources[0],
                                 'criterion_verified': False}])
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'active')
        self.run_reply(self.transition('done', result=result),
                       replies=(self.assessment(), self.assessment('evaluation')))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'done')
        self.assertEqual(self.catalog()['intention_results'], {})

    def test_wrong_old_hidden_and_deferred_results_not_offered(self):
        source = self.read()
        # Различимая свежесть: четыре результата конкурируют за три места каталога.
        # При замороженных часах порядок равных кандидатов зависит от случайных ID.
        self.time.advance(1000)
        self.write('Старый текст')
        self.run_reply(self.goal(source))
        self.time.advance(1000)
        self.write('Другой проект', project='other')
        self.assertEqual(self.catalog()['intention_results'], {})
        self.time.advance(1000)
        result = self.write('Поздний результат')
        catalog = self.catalog()
        self.assertIn(result.memory_id, [item['memory_id'] for item in catalog['world_results']])
        self.assertIn('my_goal', catalog['intention_results'])
        self.m.off = True
        self.assertEqual(self.catalog()['intention_results'], {})
        self.m.off = False
        self.assertTrue(self.m.fade(result.memory_id).applied)
        self.assertEqual(self.catalog()['intention_results'], {})
        self.time.advance(1000)
        self.write('Ещё один результат')
        self.run_reply(self.transition('deferred'))
        self.assertEqual(self.catalog()['intention_results'], {})
