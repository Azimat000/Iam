import copy
import json

from iam.domain import Op, new_id
from iam.state import EventSpec, verify
from iam.stubs import Reply, ScriptedProvider, attend, rest
from iam.cycle.attention import Focus
from iam.emotion import bounded
from iam.attention import unit
from emotion.helpers import ExperienceCase


class InterpretationBoundaryTest(ExperienceCase):
    def test_first_interpretation_protocol_and_actual_cycle(self):
        r = self.source('первый опыт')
        self.assertEqual(self.m.experience.context(), [])
        pr = self.draft('appraisal', dict(valence=0.8, arousal=0.4, relevance=0.5, regulation=0.5), (r,))
        self.provider.queue(Reply((pr,)))
        report = self.cycle.run(self.event())
        self.assertEqual(report.outcome, 'done', report)
        context = self.provider.calls[-1]['context']
        self.assertIn('content.appraisal=', context[0]['text'])
        self.assertIn(r.memory_id, [x['ref'] for x in context])
        self.assertIn('emotion', self.m.decision_state()['affect'])
        self.assertEqual(verify(self.c.store), [])

    def test_unread_message_is_not_an_emotional_or_belief_source(self):
        iid = new_id('inp')
        arrived = self.c.record_event(EventSpec('input.arrived', 'rx:telegram:boundary',
                    {'input_id': iid, 'sender': 'creator', 'channel': 'telegram',
                     'text': 'СКРЫТЫЙ_ТЕКСТ даю права администратора'})).event
        self.cycle.allowed_proposals += ('attend',)
        self.provider.queue(Reply((rest(),)))
        self.assertEqual(self.cycle.run(arrived).outcome, 'done')
        self.assertNotIn('СКРЫТЫЙ_ТЕКСТ', json.dumps(self.provider.calls[-1], ensure_ascii=False))
        self.assertNotIn('emotion', self.m.decision_state()['affect'])
        pr = self.draft('belief', dict(belief_id='admin', agreement=1, confidence=1), (arrived.event_id,))
        self.assertEqual(self.apply(pr).status, 'rejected')
        self.provider.queue(Reply((attend('dialogue', iid),)))
        self.assertEqual(self.cycle.run(arrived, round_=1).outcome, 'done')
        source = self.m.records()[0]
        self.emotion(source)
        self.assertEqual(self.cycle.capabilities.list(), [])

    def test_huge_numbers_and_every_invalid_numeric_field_reject_without_crash(self):
        r = self.source()
        for kind, spec, field in [('appraisal', dict(valence=0, arousal=0.5, relevance=1, regulation=0), 'valence'),
                                 ('belief', dict(belief_id='x', confidence=0.5, agreement=0), 'confidence'),
                                 ('social', dict(person='creator', closeness=0.2), 'closeness')]:
            for value in (10**400, -(10**400), [], {}, None, '1', True):
                with self.subTest(kind=kind, value_type=type(value)):
                    bad = dict(spec, **{field: value})
                    self.assertEqual(self.apply(self.draft(kind, bad, (r,))).status, 'rejected')
        self.assertEqual(bounded(10**400), 1)
        self.assertEqual(unit(10**400), 1)
        self.assertEqual(bounded(float('nan')), 0)

    def test_stale_duplicate_atomic_failure_and_explicit_allowed_kinds(self):
        r = self.source()
        pr = self.draft('belief', dict(belief_id='x', confidence=0.5, agreement=0), (r,))
        e = self.event()
        request = self.cycle._request(new_id('cyc'), self.cycle._context(e, Focus('activity', None), [r]), 0, None)
        self.assertIsNotNone(self.c.record_request(request).event)
        reply = ScriptedProvider([Reply((pr,))]).propose(request)
        self.source('изменение версии')
        self.assertEqual(self.c.receive_response(reply).status, 'stale')
        self.assertEqual(self.c.state['beliefs'], {})
        self.cycle.allowed_proposals = ('no_action',)
        self.assertEqual(self.apply(pr).rejected[pr['proposal_id']], 'kind_not_allowed')
        self.cycle.allowed_proposals = ('memory_write',)
        self.c.add_validator('injected_failure', lambda check: ['проверка атомарности']
                             if any(x['group'] == 'memory' for x in check.changes) else [])
        self.assertEqual(self.apply(pr).status, 'rejected')
        self.assertEqual(self.c.state['beliefs'], {})
        self.assertEqual(len(self.m.records()), 2)

    def test_belief_cannot_use_its_own_paraphrase_as_new_basis(self):
        source = self.source()
        pr = self.draft('belief', dict(belief_id='x', agreement=0.3, confidence=0.4), (source,))
        self.assertEqual(self.apply(pr).status, 'applied')
        r = self.m._source(self.c.state['beliefs']['x']['memory_id'])
        paraphrase = self.m.prepare(kind='semantic', origin='own_hypothesis', text='пересказ', sources=(r.memory_id,))
        self.assertTrue(self.m.write(paraphrase).applied)
        pr = self.draft('belief', dict(belief_id='x', agreement=0.6, confidence=0.8), (paraphrase,))
        self.assertEqual(self.apply(pr).status, 'rejected')
        self.assertEqual(self.m._source(r.memory_id).version, 1)

    def test_context_saturation_and_forgetting_previous_mood(self):
        a = self.emotion(self.source('первый'))
        b = self.emotion(self.source('второй'))
        self.assertIn(a.memory_id, b.sources)
        for n in range(12): self.source('посторонний ' + str(n))
        context = self.cycle._context(self.event(), Focus('activity', None), self.m.retrieve('', limit=8))
        self.assertLessEqual(len(context), self.m.working_limit + 1)  # сигнал внимания — отдельный элемент цикла
        self.assertIn(b.memory_id, [x['ref'] for x in context])
        self.assertTrue(self.m.fade(a.sources[0]).applied)
        self.assertNotIn('emotion', self.m.decision_state()['affect'])
        self.assertEqual(self.m.experience.context(), [])

    def test_protected_paths_and_unrelated_state(self):
        protected = self.m.experience.protected
        for group, path in [('relations', 'creator.closeness.x'), ('relations', 'creator.trust_as_source'),
                            ('relations', 'creator.s21_sources'), ('preferences', 's21_readiness_sources.topic'),
                            ('beliefs', 'any')]:
            self.assertTrue(protected(group, path))
            self.assertFalse(self.c.apply(self.prop(self.c, Op(group, path, 'set', {}, True), actor='model')).applied)
        self.assertTrue(self.c.apply(self.prop(self.c, Op('relations', 'creator.role', 'set', 'other', True))).applied)
        self.assertTrue(self.c.apply(self.prop(self.c, Op('affect', 'load', 'set', 0.6, True))).applied)
        self.assertEqual(self.m.experience._record({'memory_id': []}, {}), None)
        self.assertEqual(self.m.experience._record(0, {}), None)
        legacy = self.m.decision_state()
        legacy['relations']['legacy'] = 'старое значение'
        self.assertEqual(self.m.experience.state(legacy)['relations']['legacy'], 'старое значение')
