import json

from iam.clock import life_moment
from iam.domain import new_id
from iam.world import PersistentWorld, CAPABILITIES, READ, WRITE
from iam.world.cycle import LivingCycle, WorldMemory
from iam.stubs import ScriptedProvider, StubCapabilities, StubCreatorBridge, Reply, act
from state.helpers import StateCase


class LivingCase(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller()
        self.world = PersistentWorld(self.c)
        self.world.initialize()
        self.material = self.world.add_material('Короткая история', 'Снег тает у окна. Свет ложится на стол.', 'Тестовая библиотека')
        self.m = WorldMemory(self.c)
        self.p = ScriptedProvider()
        self.caps = StubCapabilities(CAPABILITIES)
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.assertTrue(self.c.start_session().applied)

    def due(self):
        self.assertTrue(self.c.schedule_moment(life_moment(new_id('mom'), self.c.life_time_ms, 'world')).applied)
        return self.c.fire_due_moments()[0].events[0]

    def run_reply(self, *proposals, replies=()):
        self.p.queue(Reply(tuple(proposals)), *replies)
        return self.cycle.run(self.due())

    def read(self):
        report = self.run_reply(act(READ, {'material_id': self.material}))
        self.assertEqual(report.actions[0].status, 'confirmed', report)
        return self.c.store.get_memory(report.actions[0].memory_id)

    def write(self, value='Свет у окна напоминает о весне.', project='poem'):
        report = self.run_reply(act(WRITE, {'project_id': project, 'title': 'Моя работа', 'text': value}))
        self.assertEqual(report.actions[0].status, 'confirmed', report)
        return self.c.store.get_memory(report.actions[0].memory_id)

    def goal(self, source, iid='my_goal', project='poem', text='Написать стихотворение о свете', criterion='Есть две строки о свете', operation='write'):
        return {'proposal_id':new_id('prop'), 'kind':'intention', 'rationale':'Продолжить тему прочитанного',
                'cites':[source.memory_id], 'body':{'op':'create', 'intention_id':iid, 'text':text,
                                                 'criterion':criterion,'project_id':project,'operation':operation}}

    def transition(self, status, iid='my_goal', result=None):
        body = {'op':'transition','intention_id':iid,'to_status':status}
        cites = []
        if result is not None:
            body['result_ref'] = result.memory_id
            cites = [result.memory_id, *result.sources]
        return {'proposal_id':new_id('prop'), 'kind':'intention', 'rationale':'Следующий этап дела', 'cites':cites, 'body':body}

    @staticmethod
    def assessment(phase='requirement', kind='artifact', verdict='pass', quote=None):
        def reply(request):
            packet = json.loads(request['context'][1]['text'])
            if phase == 'requirement':
                body = {'required_kind':kind,'support_quote':quote if quote is not None else packet['goal']['text'],'reason':'Проверка вида результата'}
            else:
                body = {'verdict':verdict,'goal_met':True,'criterion_met':True,'evidence_sufficient':True,
                        'support_quote':quote if quote is not None else packet['available_result']['text'][:100], 'reason':'Проверка исходной цели'}
            return Reply(({'proposal_id':new_id('prop'),'kind':'no_action','rationale':'Отдельная оценка',
                           'cites':[request['context'][1]['ref']], 'body':body},))
        return reply
