import json

from iam.domain import new_id
from iam.stubs import Reply, StubCreatorBridge, act, rest
from iam.world import PersistentWorld, WRITE
from iam.world.cycle import LivingCycle, WorldMemory
from motivation.test_practice import practice
from world.helpers import LivingCase


class PracticeTransferTest(LivingCase):
    def test_two_projects_teach_a_method_usable_after_restart_without_completing_goals(self):
        source = self.read()
        for index, project in enumerate(('window', 'station')):
            iid = 'goal' + str(index)
            self.run_reply(self.goal(source, iid=iid, project=project))
            result = self.write('Свет на ' + project + '.\nТень подчёркивает его границу.', project=project)
            self.run_reply(practice(result, iid=iid), replies=(self.assessment(), self.assessment('evaluation')))
        learning = self.cycle.motivation.learning
        self.assertEqual(set(self.c.state['intentions'][iid]['status'] for iid in ('goal0', 'goal1')), {'active'})
        samples = learning.measurements()['image_to_line']
        self.assertEqual(samples['estimate'], 1)
        self.assertEqual({sample['project_id'] for sample in samples['samples']}, {'window', 'station'})
        self.assertFalse(samples['generalized_skill_verified'])
        refs = list(learning.assessments())
        method = {'proposal_id': new_id('prop'), 'kind': 'memory_write', 'rationale': 'Обобщаю две пробы',
                  'cites': refs, 'body': {'draft': {'kind': 'procedural', 'origin': 'own_hypothesis',
                  'content': {'text': 'Связать две строки одним видимым образом',
                              'procedure': {'skill_id': 'image_to_line', 'method': 'Образ в первой строке, его изменение во второй'}}}}}
        self.assertEqual(self.run_reply(method).outcome, 'done')
        method_ref = learning.procedures()['image_to_line']['source']
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        self.m, self.world = WorldMemory(self.c), PersistentWorld(self.c)
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.c.resume_session()

        def use_method(request):
            blocks = [json.loads(item['text']) for item in request['context']
                      if item['ref'] == method_ref and item['text'].startswith('{')]
            self.assertTrue(any(block.get('procedure', {}).get('skill_id') == 'image_to_line' for block in blocks))
            return Reply((act(WRITE, {'project_id': 'train', 'title': 'Отъезд',
                                     'text': 'Свет задержался на окне.\nПоезд увёз его за поворот.'}, cites=[method_ref]),))

        self.p.queue(use_method)
        report = self.cycle.run(self.due())
        self.assertEqual(report.actions[0].status, 'confirmed')
        self.assertIn('Поезд увёз', self.world.state['actions'][report.actions[0].action_id]['outcome'])
        self.assertTrue(self.m.fade(refs[0]).applied)
        self.assertFalse(self.cycle.motivation.learning.procedures())
        self.run_reply(rest())
        self.assertNotIn(method_ref, {item['ref'] for item in self.p.calls[-1]['context']})
