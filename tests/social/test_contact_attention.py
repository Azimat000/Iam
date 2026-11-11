"""Ключ отношения соответствует реально пришедшему уведомлению, а не тестовому псевдониму."""
import json
from types import SimpleNamespace

from iam.attention import AttentionPolicy
from iam.cycle import Cycle
from iam.domain import new_id
from iam.domain.contacts import contact_id
from iam.social import SocialMemory
from iam.stubs import Reply, StubCapabilities, attend, rest
from iam.state import verify
from perception.test_perception import PerceptionCase


class ContactAttentionTest(PerceptionCase):
    def test_acquired_creator_relation_reaches_next_telegram_choice_and_fades_with_source(self):
        self.memory = SocialMemory(self.c)
        self.cycle = Cycle(self.c, self.provider, self.memory, self.world, StubCapabilities(), self.creator,
                           allowed_proposals=('attend', 'no_action', 'memory_write'))
        self.p = self.cycle.perception
        iid, _ = self.read('Я рядом, можешь не торопиться')
        source = self.memory.records()[0]
        relation = {'proposal_id':new_id('prop'), 'kind':'memory_write', 'rationale':'Опыт поддержки',
                    'cites':[source.memory_id], 'body':{'draft':{'kind':'semantic','origin':'own_hypothesis',
                    'content':{'text':'Мне спокойно рядом с создателем',
                               'social':{'person':'creator','closeness':.85,'trust_as_source':.2}}}}}
        self.provider.queue(Reply((relation,)))
        self.assertEqual(self.cycle.run(self.c.find_event(key='rd:' + iid)).outcome, 'done')
        self.assertEqual(self.memory.overview()['people']['creator']['closeness'], .85)
        _, event = self.arrive('СОДЕРЖАНИЕ_ЕЩЁ_СКРЫТО', 2)
        self.run_reply(event, rest())
        signal = next(json.loads(item['text']) for item in self.provider.calls[-1]['context']
                      if item['text'].startswith('{') and 'relationship_signal' in json.loads(item['text']))
        self.assertEqual(signal['relationship_signal'], .85)
        self.assertNotIn('СОДЕРЖАНИЕ_ЕЩЁ_СКРЫТО', json.dumps(self.provider.calls[-1], ensure_ascii=False))
        self.assertEqual(self.c.state['relations']['creator']['trust_as_source'], .2)
        self.assertTrue(self.memory.fade(source.memory_id).applied)
        self.assertEqual(AttentionPolicy().signals(self.memory.decision_state(), self.p.notices())['relationship_signal'], 0)
        self.assertEqual(verify(self.c.store), [])

    def test_other_channels_use_the_same_identity_and_n1_discloses_no_relationship(self):
        channel, sender = 'local', '123'
        person = SocialMemory.contact(SimpleNamespace(payload={'channel':channel, 'sender':sender}))
        self.assertEqual(person, contact_id(channel, sender))
        state = self.memory.decision_state()
        state['relations'][person] = {'closeness':.4}
        policy = AttentionPolicy()
        self.assertEqual(policy.signals(state, {'count':1,'items':[{'channel':channel,'sender':sender}]})['relationship_signal'], .4)
        self.assertEqual(policy.signals(state, {'count':1})['relationship_signal'], 0)
        self.assertEqual(contact_id('telegram', 123), contact_id('telegram', '123'))
