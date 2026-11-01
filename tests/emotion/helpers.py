from iam.cycle import Cycle
from iam.domain import new_id
from iam.memory import PersistentMemory
from iam.state import EventSpec
from iam.stubs import Reply, ScriptedProvider, StubCapabilities, StubCreatorBridge, StubWorld
from state.helpers import StateCase


class ExperienceCase(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)
        self.m = PersistentMemory(self.c)
        self.provider = ScriptedProvider()
        self.cycle = Cycle(self.c, self.provider, self.m, StubWorld(), StubCapabilities(), StubCreatorBridge(),
                           allowed_proposals=('memory_write', 'no_action', 'state_change'))

    def event(self):
        from iam.domain import Op
        result = self.c.apply(self.prop(self.c, Op('world_time', 'world.test', 'set', new_id('evt'), True),
                              actor='world', carrier=EventSpec('activity.completed', 'act:' + new_id('act') + ':done')))
        self.assertTrue(result.applied, result)
        return result.events[0]

    def source(self, text='критика', group='speaker'):
        event = self.event()
        r = self.m.prepare(kind='episodic', origin='stated_by_other', text=text, sources=(event.event_id,),
                           claim_kind='fact_claim', independence_group=group)
        self.assertTrue(self.m.write(r).applied)
        return r

    def draft(self, kind, spec, sources, text='Интерпретация'):
        return {'proposal_id': new_id('prop'), 'kind': 'memory_write', 'rationale': 'осмысление опыта',
                'cites': [r.memory_id if hasattr(r, 'memory_id') else r for r in sources],
                'body': {'draft': {'kind': 'emotional_associative' if kind == 'appraisal' else 'semantic',
                                  'origin': 'own_hypothesis', 'content': {'text': text, kind: spec}}}}

    def apply(self, pr, *, visible=None):
        # Настоящие record_request/receive_response и все валидаторы ядра.
        from iam.cycle.attention import Focus
        event = self.event()
        rows = list(self.m.records()) if visible is None else visible
        rows = sorted(rows, key=lambda r: r.memory_id not in pr['cites'])
        context = self.cycle._context(event, Focus('activity', None), rows)
        request = self.cycle._request(new_id('cyc'), context, 0, None)
        self.assertIsNotNone(self.c.record_request(request).event)
        response = ScriptedProvider([Reply((pr,))]).propose(request)
        return self.c.receive_response(response)

    def emotion(self, source, **kw):
        spec = dict(valence=-0.8, arousal=0.6, relevance=0.5, regulation=0.75)
        spec.update(kw)
        pr = self.draft('appraisal', spec, (source,))
        result = self.apply(pr)
        self.assertEqual(result.status, 'applied', result)
        return self.m._source(self.c.state['affect']['emotion']['memory_id'])
