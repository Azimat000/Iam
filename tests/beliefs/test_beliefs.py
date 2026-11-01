import copy
import json

from iam.domain import Op
from iam.state import verify
from emotion.helpers import ExperienceCase


class BeliefTest(ExperienceCase):
    def belief(self, sources, **kw):
        spec = dict(belief_id='claim', agreement=-0.7, confidence=0.4)
        spec.update(kw)
        result = self.apply(self.draft('belief', spec, sources, 'Я могу ошибаться'))
        self.assertEqual(result.status, 'applied', result)
        return self.m._source(self.c.state['beliefs'][spec['belief_id']]['memory_id'])

    def test_four_quantities_change_independently_and_claim_is_not_truth(self):
        source = self.source('оператор утверждает X')
        r = self.belief((source,))
        for field, value in [('closeness', 0.95), ('trust_as_source', 0.9), ('readiness', 0.2)]:
            spec = {'person': 'creator', field: value}
            if field == 'readiness': spec['topic'] = 'claim'
            before = copy.deepcopy(self.c.state)
            self.assertEqual(self.apply(self.draft('social', spec, (source,))).status, 'applied')
            self.assertEqual(before['beliefs'], self.c.state['beliefs'])
        state = self.m.decision_state()
        self.assertEqual(state['relations']['creator']['closeness'], 0.95)
        self.assertEqual(state['relations']['creator']['trust_as_source'], 0.9)
        self.assertEqual(state['beliefs']['claim']['agreement'], -0.7)
        self.assertEqual(state['preferences']['change_readiness']['claim'], 0.2)
        desc = self.m.experience.context()[-1]['text']
        self.assertIn('Интерпретация', desc)
        self.assertEqual(json.loads(desc)['independent_support'], 0)
        self.assertEqual(r.origin, 'own_hypothesis')
        self.assertEqual(verify(self.c.store), [])

    def test_repeated_criticism_changes_emotion_without_new_support(self):
        a, b = self.source('критика'), self.source('повтор критики')
        rec = self.belief((a,))
        self.emotion(a)
        before = self.m.decision_state()['affect']['emotion']['mood']
        rec2 = self.belief((a, b), confidence=0.6)
        self.emotion(b)
        after = self.m.decision_state()['affect']['emotion']['mood']
        self.assertLess(after, before)
        self.assertEqual(rec2.memory_id, rec.memory_id)
        self.assertEqual(rec2.version, 2)
        self.assertEqual(len(rec2.history), 2)
        self.assertEqual(self.m.support(rec2.sources)['unknown_groups'], ['speaker'])
        self.assertEqual(self.m.support(rec2.sources)['independent_support'], 0)
        c = self.source('независимость неизвестна', 'other')
        self.assertEqual(self.m.support((a.memory_id, c.memory_id))['independent_support'], 0)
        self.assertTrue(self.m.confirm_independence(['speaker', 'other'], reason='проверены внешние источники').applied)
        rec3 = self.belief((a, b, c))
        self.assertEqual(self.m.support(rec3.sources)['independent_support'], 2)
        from iam.beliefs import describe
        self.assertEqual(describe(rec3, self.m)['independent_support'], 2)

    def test_correction_forgetting_and_off_remove_beliefs_and_social_caches(self):
        source = self.source('СЕКРЕТ_МИРА')
        r = self.belief((source,))
        pr = self.draft('social', dict(person='creator', closeness=0.8, trust_as_source=0.6,
                                      readiness=0.3, topic='claim'), (source,))
        self.assertEqual(self.apply(pr).status, 'applied')
        self.assertEqual(self.m.decision_state()['preferences']['change_readiness']['claim'], 0.3)
        self.m.off = True
        self.assertEqual(self.m.experience.context(), [])
        self.assertEqual(self.m.decision_state()['beliefs'], {})
        self.assertNotIn('closeness', self.m.decision_state()['relations']['creator'])
        self.m.off = False
        self.assertTrue(self.m.fade(source.memory_id).applied)
        state = self.m.decision_state()
        self.assertEqual(state['beliefs'], {})
        self.assertNotIn('closeness', state['relations'].get('creator', {}))
        self.assertNotIn('claim', state['preferences'].get('change_readiness', {}))
        self.assertNotIn(r.memory_id, [r.memory_id for r in self.m.records()])

    def test_invalid_social_belief_and_self_support(self):
        source = self.source()
        variants = [('belief', {'belief_id': '..', 'confidence': 1, 'agreement': 0}),
                    ('belief', {'belief_id': 'x', 'confidence': True, 'agreement': 0}),
                    ('belief', {'belief_id': 'x', 'confidence': 1, 'agreement': 2}),
                    ('social', {'person': 'x', 'readiness': 0.2}),
                    ('social', {'person': 'x', 'closeness': -1}),
                    ('social', {'person': '..', 'closeness': 1}),
                    ('social', {'person': 'x', 'closeness': 0.1, 'topic': 'unused'}),
                    ('social', {'person': 'x', 'admin': 1}), ('social', {'person': 'x'})]
        for kind, spec in variants:
            self.assertEqual(self.apply(self.draft(kind, spec, (source,))).status, 'rejected')
        r = self.belief((source,))
        self.assertEqual(self.apply(self.draft('belief', dict(belief_id='claim', agreement=1, confidence=1), (r,))).status, 'rejected')
