import json
from iam.domain import new_id
from iam.cycle.runner import input_id_for
from iam.social import SocialMemory
from iam.state import EventSpec, verify
from iam.stubs import Reply, StubCreatorBridge, attend, rest
from iam.world.cycle import LivingCycle
from iam.world.options import OptionsCycle
from world.helpers import LivingCase


class SocialDevelopmentTest(LivingCase):
    def enable(self, prospective=False):
        self.m = SocialMemory(self.c)
        cls = OptionsCycle if prospective else LivingCycle
        kwargs = {'fast_inputs': lambda: True} if prospective else {}
        self.cycle = cls(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge(), **kwargs)

    def incoming(self):
        iid = input_id_for('telegram', '1')
        arrival = self.c.record_event(EventSpec('input.arrived', 'rx:telegram:1',
            {'input_id': iid, 'sender': '123', 'channel': 'telegram', 'text': 'Мне неприятна эта тема, давай позже.'})).event
        self.p.queue(Reply((attend('dialogue', iid),)))
        self.assertEqual(self.cycle.run(arrival).outcome, 'done')
        row = next(row for row in self.m.records() if row.origin == 'stated_by_other')
        return row, self.c.find_event(key='rd:'+iid)

    def social(self, row):
        return {'proposal_id': new_id('prop'), 'kind': 'memory_write', 'rationale': 'Осмыслила разговор',
                'cites': [row.memory_id], 'body': {'draft': {'kind': 'semantic', 'origin': 'own_hypothesis',
                'content': {'text': 'Готова вернуться к теме позже', 'social': {
                    'person': 'creator', 'topic': 'personal', 'readiness': .3, 'trust_as_source': .6}}}}}

    def readiness(self):
        return [json.loads(item['text']) for item in self.p.calls[-1]['context']
                if item['text'].startswith('{') and json.loads(item['text']).get('kind') == 'social_readiness']

    def test_fast_current_read_exposes_optional_exact_social_route(self):
        self.enable(prospective=True)
        row, trigger = self.incoming()
        self.assertNotIn('social_update_option', json.dumps(self.p.calls[-1]['context']))
        before = len(self.p.calls)
        def choose(request):
            options = [json.loads(item['text']) for item in request['context'] if
                       item['text'].startswith('{') and json.loads(item['text']).get('kind') == 'social_update_option']
            self.assertEqual(len(options), 1)
            option = options[0]
            self.assertEqual((option['person'], option['source']), ('creator', row.memory_id))
            self.assertEqual(option['format']['cites'], [row.memory_id])
            self.assertEqual(option['format']['body']['draft']['content']['social'], {'person': 'creator'})
            self.assertIn('topic', option['optional_fields'])
            return Reply((self.social(row),))
        self.p.queue(choose)
        self.assertEqual(self.cycle.run(trigger).outcome, 'done')
        self.assertEqual(len(self.p.calls), before + 1)
        self.assertEqual(self.p.calls[-1]['purpose'], 'choose_action')
        self.assertEqual(self.c.state['relations']['creator']['trust_as_source'], .6)
        self.assertTrue(self.m.fade(row.memory_id).applied)
        self.p.queue(Reply((rest(),)))
        self.cycle.run(trigger, round_=1)
        self.assertNotIn('social_update_option', json.dumps(self.p.calls[-1]['context']))

    def test_readiness_is_used_in_next_request_and_disappears_after_revision(self):
        self.enable()
        row, trigger = self.incoming()
        self.p.queue(Reply((self.social(row),)))
        self.assertEqual(self.cycle.run(trigger).outcome, 'done')
        self.run_reply(rest())
        block = self.readiness()[0]
        self.assertEqual((block['topic'], block['readiness'], block['person']), ('personal', .3, 'creator'))
        self.m.off = True
        self.run_reply(rest())
        self.assertEqual(self.readiness(), [])
        self.m.off = False
        self.assertTrue(self.m.revise(block['source'], 'Оценка пересмотрена', sources=(row.memory_id,), reason='Пересмотр').applied)
        self.run_reply(rest())
        self.assertEqual(self.readiness(), [])
        self.assertEqual(verify(self.c.store), [])

    def test_fast_read_is_reviewed_later_without_forcing_a_relationship(self):
        self.enable(prospective=True)
        row, trigger = self.incoming()
        self.p.queue(Reply((rest(),)))
        self.assertEqual(self.cycle.run(trigger).outcome, 'done')
        self.assertNotIn('appraise', [q['purpose'] for q in self.p.calls])
        self.assertEqual(self.c.state['relations'], {})
        self.run_reply(self.social(row), replies=(Reply((rest(),)),))
        self.assertEqual([q['purpose'] for q in self.p.calls[-2:]], ['appraise', 'choose_action'])
        self.assertEqual(self.c.state['relations']['creator']['trust_as_source'], .6)
        self.assertNotIn('closeness', self.c.state['relations']['creator'])
        self.assertEqual(self.readiness()[0]['readiness'], .3)
        self.run_reply(rest())
        self.assertEqual(self.p.calls[-1]['purpose'], 'choose_action')
        self.assertTrue(self.m.fade(row.memory_id).applied)
        self.run_reply(rest())
        self.assertEqual(self.readiness(), [])

    def test_social_review_can_be_declined_and_small_context_omits_readiness(self):
        self.enable(prospective=True)
        row, trigger = self.incoming()
        self.p.queue(Reply((rest(),)))
        self.cycle.run(trigger)
        self.run_reply(replies=(Reply((rest(),)),))
        self.assertEqual(self.c.state['relations'], {})
        self.assertEqual(self.c.state['beliefs'], {})
        self.run_reply(self.social(row))
        self.run_reply(rest())
        self.assertEqual(len(self.readiness()), 1)
        self.m.text_limit = 100
        self.run_reply(rest())
        self.assertEqual(self.readiness(), [])
