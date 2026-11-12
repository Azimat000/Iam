import json
from iam.domain import new_id
from iam.stubs import StubCreatorBridge, Reply, act
from iam.world import PersistentWorld, WRITE
from iam.world.cycle import LivingCycle, WorldMemory
from world.helpers import LivingCase


class LearningContextTest(LivingCase):
    def test_return_to_first_skill_prioritizes_its_latest_assessment(self):
        for n, project in enumerate(('poem', 'second', 'third', 'poem'), 1):
            self.time.advance(1000)
            source = self.read()
            iid = 'goal' + str(n)
            self.run_reply(self.goal(source, iid=iid, project=project))
            result = self.write('Свет у окна ' + str(n) + '.\nВесна у порога.', project=project)
            self.run_reply(self.transition('done', iid=iid, result=result),
                           replies=(self.assessment(), self.assessment('evaluation')))
        learning = self.cycle.motivation.learning
        shown = [json.loads(item['text'])['skill_assessment'] for item in learning.context()]
        self.assertEqual({item['skill_id'] for item in shown}, {'third', 'poem'})
        self.assertEqual(sum(item['skill_id'] == 'poem' for item in shown), 2)
        refs = {row.memory_id for row in learning.assessments().values()
                if row.content['skill_assessment']['skill_id'] == 'poem'}
        self.write('Поздняя работа', project='later')
        self.assertTrue(refs <= {item['ref'] for item in self.p.calls[-1]['context']})

    def test_old_assessments_allow_learning_after_restart_and_later_use(self):
        source = self.read()
        for n in (1, 2):
            self.run_reply(self.goal(source, iid='goal'+str(n)))
            result = self.write('Свет у окна '+str(n)+'.\nВесна у порога.')
            self.run_reply(self.transition('done', iid='goal'+str(n), result=result),
                           replies=(self.assessment(), self.assessment('evaluation')))
        refs = [row.memory_id for row in self.m.records() if 'skill_assessment' in row.content]
        for _ in range(6):
            self.time.advance(1000)
            self.read()
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        self.world, self.m = PersistentWorld(self.c), WorldMemory(self.c)
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.c.resume_session()
        proposal = {'proposal_id': new_id('prop'), 'kind': 'memory_write', 'rationale': 'Обобщаю опыт',
                    'cites': refs, 'body': {'draft': {'kind': 'procedural', 'origin': 'own_hypothesis',
                    'content': {'text': 'Две связанные строки', 'procedure': {'skill_id': 'poem',
                    'method': 'Сначала образ, затем связанная строка'}}}}}
        self.assertEqual(self.run_reply(proposal).outcome, 'done')
        self.assertTrue(set(refs) <= {item['ref'] for item in self.p.calls[-1]['context']})
        learning = self.cycle.motivation.learning
        self.assertIn('poem', learning.procedures())
        method_ref = learning.procedures()['poem']['source']
        self.time.advance(1000)
        def apply_method(request):
            blocks = [json.loads(item['text']) for item in request['context']
                      if item['ref'] == method_ref and item['text'].startswith('{')]
            self.assertTrue(any(block.get('procedure', {}).get('method') ==
                                'Сначала образ, затем связанная строка' for block in blocks))
            choice = act(WRITE, {'project_id': 'later', 'title': 'Новая работа',
                                'text': 'Свет на стекле.\nОкно удержало рассвет.'})
            choice['cites'] = [method_ref]
            return Reply((choice,))
        self.p.queue(apply_method)
        report = self.cycle.run(self.due())
        self.assertEqual(report.actions[0].status, 'confirmed')
        self.assertEqual(self.p.calls[-1]['purpose'], 'choose_action')
        blocks = learning.context()
        self.assertTrue(any('procedure' in json.loads(item['text']) for item in blocks))
        self.assertTrue(any('skill_assessment' in json.loads(item['text']) for item in blocks))
        self.m.text_limit = 100
        self.assertEqual(learning.context(), [])
        self.m.text_limit = 2000
        self.cycle.motivation.off = True
        self.assertEqual(learning.context(), [])
        self.cycle.motivation.off = False
        self.assertTrue(self.m.fade(refs[0]).applied)
        self.assertFalse(learning.procedures())
        self.assertNotIn(refs[0], {item['ref'] for item in learning.context()})
        self.m.off = True
        self.assertEqual(learning.context(), [])
