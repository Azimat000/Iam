import dataclasses
import unittest

from iam.memory import PersistentMemory
from iam.domain import MEMORY_KINDS, Link, Op, MemoryRecord
from iam.interfaces import ActionRequest, ActionOutcome
from iam.state import EventSpec
from state.helpers import StateCase


class MemoryTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)
        self.m = PersistentMemory(self.c)

    def record(self, text='чай', **kw):
        args = dict(kind='semantic', origin='own_hypothesis', text=text, sources=(self.root(self.c),))
        args.update(kw)
        return self.m.prepare(**args)

    def test_nine_kinds_restart_schema(self):
        for kind in MEMORY_KINDS:
            r = self.record(kind=kind)
            if kind == 'autobiographical':
                # Отдельная запись наблюдаемого результата.
                from iam.state import Proposal
                e = self.c.apply(self.prop(self.c, Op('world_time', 'world.result', 'set', 'test', True), actor='world', carrier=EventSpec('activity.completed', 'act:act_Test00001:done'))).events[0]
                r = self.record(kind=kind, origin='observation', sources=(e.event_id,), content={'experience':'live'})
            self.assertTrue(self.m.write(r).applied)
        self.assertEqual(len(self.m.retrieve('чай', limit=20)), 8)
        self.m = PersistentMemory(self.reopen(self.c), working_limit=20)
        self.assertEqual({r.kind for r in self.m.retrieve('чай', limit=20)}, set(MEMORY_KINDS))
        for field in ('source', 'created_at', 'origin', 'sources', 'links', 'history'):
            raw = r.to_dict(); del raw[field]
            with self.assertRaises(ValueError):
                MemoryRecord.from_dict(raw)

    def test_sources_versions_and_outcomes(self):
        r = self.record()
        base = (self.c.epoch, self.c.state_version)
        self.assertTrue(self.m.write(r).applied)
        self.assertEqual(self.m.write(self.record(), base=base).status, 'stale')
        self.assertFalse(self.m.write(self.record(sources=('evt_Missing001',))).applied)
        self.assertFalse(self.m.write(self.record(sources=('mem_Missing001',))).applied)
        self.assertFalse(self.m.write(self.record(links=(Link('cause', 'mem_Missing001'),))).applied)
        self.assertFalse(self.m.write(self.record(content={'outcome': 'confirmed'})).applied)
        self.assertFalse(self.m.write(self.record(content={'experience': 'fake'})).applied)
        self.assertFalse(self.m.write(dataclasses.replace(self.record(), created_life_ms=999999)).applied)
        self.assertFalse(self.m.write(self.record(origin='stated_by_other', independence_group='p')).applied)
        self.assertTrue(self.m.write(self.record(origin='stated_by_other', claim_kind='fact_claim', independence_group='p')).applied)
        self.assertTrue(self.m.write(self.record(sources=(r.memory_id,))).applied)

    def test_retrieval_filters_links_and_bounds(self):
        linked = self.record('кофе'); self.assertTrue(self.m.write(linked).applied)
        r = self.record(links=(Link('cause', linked.memory_id),)); self.assertTrue(self.m.write(r).applied)
        self.assertEqual({x.memory_id for x in self.m.retrieve('чай')}, {r.memory_id, linked.memory_id})
        for changes in ({'availability': 0}, {'content': {'text':'тайна', 'superseded_by':'mem_abcdefgh'}},
                        {'history': ({'version': 1, 'at_seq': 1, 'change':'x', 'superseded_by':'mem_abcdefgh'},)}):
            hidden = dataclasses.replace(self.record('тайна'), **changes)
            self.assertTrue(self.m.write(hidden).applied)
            self.assertNotIn(hidden.memory_id, [x.memory_id for x in self.m.retrieve('')])
        for importance in ('bad', float('inf'), -1, 5):
            # Невалидные числа не пишем в JSON; ранжирование проверяется на копии read API.
            records = [dataclasses.replace(r, content={'text':'чай', 'importance':importance})]
            original = self.m.records; self.m.records = lambda: records
            self.assertTrue(self.m.retrieve(''))
            self.m.records = original
        self.assertEqual(self.m.retrieve('nothing'), [])
        self.assertEqual(self.m.retrieve('чай', limit=0), [])
        with self.assertRaises(ValueError): self.m.retrieve('', limit=-1)
        self.m.off = True
        self.assertEqual(self.m.retrieve(''), [])
        self.assertIsNone(self.m.remember_outcome(None, None, None))
        for field in ('working_limit','sensory_limit','sensory_ttl_ms','text_limit'):
            with self.assertRaises(ValueError): PersistentMemory(self.c, **{field:0})

    def test_context_sensory_ttl(self):
        m = PersistentMemory(self.c, working_limit=2, sensory_limit=2, text_limit=500)
        r = self.record('чай длинный')
        for i in range(4):
            ctx = m.context(None, {'ref': str(i), 'text':'уведомление'}, [r]*5)
            self.assertEqual(len(ctx), 2)
            self.assertTrue(all(len(x['text']) <= 500 for x in ctx))
        self.assertEqual(len(m.sensory), 2)
        self.time.advance(60001)
        self.assertEqual(m.sensory, [])
        m.off = True
        self.assertEqual(len(m.context(None, {'ref':'x','text':'x'}, [r])), 1)

    def test_hidden_sources_and_model(self):
        arrived = self.c.record_event(EventSpec('input.arrived', 'rx:test:1', {'secret':'hidden'})).event
        self.assertIsNone(self.m._source(arrived.event_id))
        r = self.record(); self.assertTrue(self.m.write(r).applied)
        self.assertFalse(self.c.apply(self.prop(self.c, Op('memory', r.memory_id, 'set', r.to_dict(), True), actor='model')).applied)
        from iam.state import KindContext
        ctx = KindContext({'context':[{'ref':r.memory_id}]}, {}, (), 0)
        proposal = {'cites':[r.memory_id], 'body':{'draft':{'kind':'semantic', 'origin':'own_hypothesis','content':{'text':'мысль'}}}}
        self.assertIsNone(self.m._proposal(proposal, ctx).violation)
        self.m.off = True
        self.assertEqual(self.m._proposal(proposal, ctx).violation.code, 'memory_off')
        self.m.off = False
        proposal['cites'] = []; self.assertIsNotNone(self.m._proposal(proposal, ctx).violation)
        proposal['cites'] = [r.memory_id]
        proposal['body']['draft']['origin'] = 'observation'; self.assertIsNotNone(self.m._proposal(proposal, ctx).violation)
        proposal['body']['draft']['origin'] = 'imagination'
        for text in ('', 5, 'x'*3000):
            proposal['body']['draft']['content']['text'] = text
            self.assertIsNotNone(self.m._proposal(proposal, ctx).violation)


if __name__ == '__main__':
    unittest.main()

class MemoryBoundaryTest(StateCase):
    record = MemoryTest.record

    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)
        self.m = PersistentMemory(self.c)

    def test_archive_never_enters_context_or_associations(self):
        r = self.record('точное совпадение')
        archive = dataclasses.replace(r, layer='archive')
        with self.c.store.transaction():
            self.c.store.add_memory(archive)
        self.assertEqual(self.m.retrieve('точное совпадение'), [])
        self.assertIsNone(self.m._source(archive.memory_id))
        for changes in ({'persona_id':'per_Other00001'}, {'origin':'simulation_event'}):
            self.assertFalse(self.m._available(dataclasses.replace(r, **changes)))
        first={'ref':'evt_First00001','text':'новое'}
        self.assertEqual(self.m.context(None, first, [archive]), [first])
        tiny = PersistentMemory(self.c, text_limit=1)
        self.assertEqual(tiny.context(None, first, [r]), [dict(first,text='н')])

    def test_provenance_cannot_be_laundered(self):
        for origin in ('model_knowledge','imagination'):
            self.assertFalse(self.m.write(self.record(origin=origin, content={'experience':'live'})).applied)
        self.assertFalse(self.m.write(self.record(kind='autobiographical')).applied)
        self.assertFalse(self.m.write(self.record(origin='observation')).applied)
        r=self.record(content={'experience':'prepared'}); self.assertTrue(self.m.write(r).applied)
        self.assertFalse(self.m.write(self.record(sources=(r.memory_id,), content={'experience':'live'})).applied)
        from iam.state import KindContext
        ctx=KindContext({'context':[{'ref':r.memory_id}]}, {}, (), 0)
        proposal={'cites':[r.memory_id], 'body':{'draft':{'kind':'autobiographical','origin':'own_hypothesis','content':{'text':'x'}}}}
        self.assertIsNotNone(self.m._proposal(proposal,ctx).violation)

    def test_hidden_fts_matches_do_not_displace_current_version(self):
        for _ in range(3):
            self.assertTrue(self.m.write(dataclasses.replace(self.record('чай'), availability=0)).applied)
            self.assertTrue(self.m.write(self.record('чай', content={'superseded_by':'mem_Current001'})).applied)
        current = self.record('прежнее слово')
        self.assertTrue(self.m.write(current).applied)
        updated = dataclasses.replace(current, version=2, content={**current.content, 'text':'чай'})
        result = self.c.apply(self.prop(self.c, Op('memory', current.memory_id, 'set', updated.to_dict(), True)))
        self.assertTrue(result.applied, result)
        result = self.m.retrieve('чай', limit=1)
        self.assertEqual([(r.memory_id, r.version, r.text) for r in result], [(current.memory_id, 2, 'чай')])
        self.assertEqual(self.m.retrieve('прежнее слово'), [])

    def test_recency_orders_equal_fts_and_importance(self):
        older = dataclasses.replace(self.record('чай'), memory_id='mem_AAAAAAAA')
        self.assertTrue(self.m.write(older).applied)
        self.time.advance(60000)
        newer = dataclasses.replace(self.record('чай'), memory_id='mem_ZZZZZZZZ')
        self.assertTrue(self.m.write(newer).applied)
        self.assertEqual([r.memory_id for r in self.m.retrieve('чай')], [newer.memory_id, older.memory_id])
        self.assertEqual(self.m.retrieve('чай', limit=1)[0].memory_id, newer.memory_id)

    def test_importance_orders_equal_fts_and_creation_time(self):
        lesser = dataclasses.replace(self.record('чай', content={'importance':0.1}), memory_id='mem_AAAAAAAA')
        greater = dataclasses.replace(self.record('чай', content={'importance':0.9}), memory_id='mem_ZZZZZZZZ')
        self.assertEqual(lesser.created_life_ms, greater.created_life_ms)
        for r in (lesser, greater):
            self.assertTrue(self.m.write(r).applied)
        self.assertEqual([r.memory_id for r in self.m.retrieve('чай')], [greater.memory_id, lesser.memory_id])
        self.assertEqual(self.m.retrieve('чай', limit=1)[0].memory_id, greater.memory_id)

    def test_multi_record_transition_history_uses_actual_event(self):
        from iam.state import verify
        one, two = self.record('первая'), self.record('вторая')
        self.c.record_event(EventSpec('input.arrived', 'rx:after:prepare', {}))
        ops = (Op('memory', one.memory_id, 'add', one.to_dict(), True),
               Op('traits', 'memory_test', 'set', 1, True),
               Op('memory', two.memory_id, 'add', two.to_dict(), True))
        proposal = self.prop(self.c, *ops)
        result = self.c.apply(proposal)
        self.assertTrue(result.applied, result)
        self.assertEqual(result.events[0].payload['proposed_transition'], proposal.transition.to_dict())
        actual_seq = result.events[0].seq
        for original in (one, two):
            stored = self.c.store.get_memory(original.memory_id)
            self.assertEqual(stored.history[0]['at_seq'], actual_seq)
            self.assertEqual(stored.history[0]['version'], 1)
            self.assertGreater(actual_seq, original.history[0]['at_seq'])
            change = next(c for c in result.changes if c['path'] == original.memory_id)
            self.assertEqual(change['after'], stored.to_dict())
        self.assertEqual(self.c.state['traits']['memory_test'], 1)
        self.assertEqual(self.c.apply(proposal).status, 'duplicate')
        altered = dataclasses.replace(proposal, transition=dataclasses.replace(proposal.transition, reason='другое'))
        self.assertEqual(self.c.apply(altered).status, 'conflict')
        reserved = self.c.apply(self.prop(self.c, Op('traits', 'x', 'set', 1, True),
                                         carrier=EventSpec('state.transition_applied', 'trn:reserved', {'proposed_transition':{}})))
        self.assertEqual(reserved.reason, 'payload_reserved')
        self.assertEqual(verify(self.c.store), [])

    def test_idempotency_of_legacy_event_without_original_proposal(self):
        from unittest.mock import patch
        original_events = self.c._events
        def legacy_events(*args, **kwargs):
            return [dataclasses.replace(event, payload={k:v for k,v in event.payload.items()
                                                        if k != 'proposed_transition'})
                    for event in original_events(*args, **kwargs)]
        proposal = self.prop(self.c, Op('traits', 'legacy', 'set', 1, True))
        with patch.object(self.c, '_events', side_effect=legacy_events):
            result = self.c.apply(proposal)
        self.assertTrue(result.applied, result)
        self.assertNotIn('proposed_transition', result.events[0].payload)
        self.assertEqual(self.c.apply(proposal).status, 'duplicate')
        altered = dataclasses.replace(proposal, transition=dataclasses.replace(proposal.transition, reason='иное'))
        self.assertEqual(self.c.apply(altered).status, 'conflict')

    def test_ordinary_transition_does_not_duplicate_proposal(self):
        from iam.state import verify
        proposal = self.prop(self.c, Op('traits', 'ordinary', 'set', 1, True))
        result = self.c.apply(proposal)
        self.assertTrue(result.applied, result)
        self.assertNotIn('proposed_transition', result.events[0].payload)
        self.assertEqual(result.events[0].payload['transition'], proposal.transition.to_dict())
        self.assertEqual(self.c.apply(proposal).status, 'duplicate')
        altered = dataclasses.replace(proposal, transition=dataclasses.replace(proposal.transition, reason='иное'))
        self.assertEqual(self.c.apply(altered).status, 'conflict')
        self.assertEqual(verify(self.c.store), [])
