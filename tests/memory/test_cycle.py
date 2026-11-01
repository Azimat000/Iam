from iam.memory import PersistentMemory
from iam.domain import new_id
from iam.state import EventSpec
from iam.stubs import Reply, act, rest
from cycle.helpers import CycleCase, CAP_NOTES


class PersistentCycleTest(CycleCase):
    def test_three_outcomes_and_off(self):
        from iam.stubs import StubWorld
        self.build(Reply(proposals=(act(CAP_NOTES),)), world=StubWorld())
        m = PersistentMemory(self.c)
        self.cycle.memory = m
        report = self.cycle.run(self.due())
        self.assertEqual(report.actions[0].status, 'confirmed')
        self.assertEqual(m.records()[0].content['outcome'], 'confirmed')
        original = m.records()[0]
        source = self.c.find_event(event_id=original.sources[0])
        forged = m.prepare(kind='episodic', origin='observation', text='wrong action', sources=(source.event_id,),
                           content={'experience':'live','outcome':'confirmed','action_id':'act_Other00001'})
        self.assertFalse(m.write(forged).applied)
        from iam.state import verify
        self.assertEqual(verify(self.c.store), [])
        for mode, status in [('fail','failed'), ('unknown','unknown')]:
            self.world.default = mode
            self.provider.queue(Reply(proposals=(act(CAP_NOTES),)))
            report = self.cycle.run(self.due())
            self.assertEqual(m.records()[-1].content['outcome'], status)
        m.off = True
        self.provider.queue(Reply(proposals=(act(CAP_NOTES),)))
        self.cycle.run(self.due())
        self.assertEqual(len(m.records()), 3)

    def test_context_experience_and_memory_off(self):
        self.build(Reply(proposals=(rest(),)))
        m = PersistentMemory(self.c)
        self.cycle.memory = m
        for text in ('дочитать главу 4 опыт первый', 'дочитать главу 4 опыт другой'):
            r = m.prepare(kind='semantic', origin='own_hypothesis', text=text, sources=(self.root(self.c),))
            self.assertTrue(m.write(r).applied)
            self.provider.queue(Reply(proposals=(rest(),)))
            self.cycle.run(self.due())
            self.assertTrue(any(text in x['text'] for x in self.provider.calls[-1]['context']))
        m.off = True
        self.cycle.run(self.due())
        self.assertEqual(len(self.provider.calls[-1]['context']), 1)

    def test_model_write_via_actual_response(self):
        self.build()
        m = PersistentMemory(self.c)
        self.cycle.memory = m
        self.cycle.allowed_proposals += ('memory_write',)
        r = m.prepare(kind='semantic', origin='own_hypothesis', text='дочитать главу 4', sources=(self.root(self.c),))
        self.assertTrue(m.write(r).applied)
        def reply(request):
            return Reply(proposals=({'proposal_id':new_id('prop'), 'kind':'memory_write', 'rationale':'мысль',
                     'cites':[r.memory_id], 'body':{'draft':{'kind':'semantic','origin':'own_hypothesis',
                                                           'content':{'text':'новая мысль'}}}},))
        self.provider.queue(reply)
        report = self.cycle.run(self.due())
        self.assertEqual(len(m.records()), 2, report)
        self.assertEqual(self.c._memory_grants, set())

    def test_memory_adapter_does_not_expand_allowed_proposals(self):
        from iam.cycle import Cycle
        self.build()
        m = PersistentMemory(self.c)
        cycle = Cycle(self.c, self.provider, m, self.world, self.caps, self.creator,
                      allowed_proposals=('no_action',))
        r = m.prepare(kind='semantic', origin='own_hypothesis', text='дочитать главу 4', sources=(self.root(self.c),))
        self.assertTrue(m.write(r).applied)
        proposal = {'proposal_id':new_id('prop'), 'kind':'memory_write', 'rationale':'мысль',
                    'cites':[r.memory_id], 'body':{'draft':{'kind':'semantic', 'origin':'own_hypothesis',
                                                          'content':{'text':'не разрешено'}}}}
        self.provider.queue(Reply(proposals=(proposal,)))
        report = cycle.run(self.due())
        self.assertEqual(cycle.allowed_proposals, ('no_action',))
        self.assertEqual(self.provider.calls[-1]['allowed_proposals'], ['no_action'])
        self.assertEqual(report.outcome, 'rejected')
        rejected = [e for e in self.c.events_after(0) if e.type == 'proposal.rejected']
        self.assertEqual(rejected[-1].payload['reason'], 'kind_not_allowed')
        self.assertEqual(len(m.records()), 1)
        self.assertEqual(self.c._memory_grants, set())

    def test_forgotten_goal_and_old_snapshot_never_reach_provider(self):
        from iam.domain import Op
        from iam.cycle.attention import Focus
        self.build(Reply(proposals=(rest(),)))
        m = PersistentMemory(self.c)
        self.cycle.memory = m
        r = m.prepare(kind='episodic', origin='own_hypothesis', text='дочитать секрет Лидия', sources=(self.root(self.c),))
        self.assertTrue(m.write(r).applied)
        self.assertTrue(self.c.apply(self.prop(self.c, Op('intentions', 'int_hidden', 'set',
                        {'text':'Лидия', 'sources':[r.memory_id]}, True))).applied)
        cached = m.retrieve('Лидия')
        self.assertTrue(m.fade(r.memory_id).applied)
        from dataclasses import replace
        trigger = replace(self.due(), payload={'owner_ref':'int_hidden'})
        self.cycle.run(trigger)
        import json
        self.assertNotIn('Лидия', json.dumps(self.provider.calls[-1], ensure_ascii=False))
        # Даже сохранённый объект до забывания вновь проверяется построителем контекста.
        ctx = self.cycle._context(trigger, Focus('intention','int_hidden','дочитать секрет Лидия'), cached)
        self.assertNotIn('Лидия', json.dumps(ctx, ensure_ascii=False))

    def test_orphan_intention_cannot_reintroduce_forgotten_partial_text(self):
        from iam.domain import Op
        from dataclasses import replace
        import json
        self.build(Reply(proposals=(rest(),)))
        m = PersistentMemory(self.c)
        self.cycle.memory = m
        r = m.prepare(kind='episodic', origin='own_hypothesis', text='имя героини Лидия', sources=(self.root(self.c),))
        self.assertTrue(m.write(r).applied)
        self.assertTrue(self.c.apply(self.prop(self.c, Op('intentions','int_orphan','set',{'text':'Лидия'},True))).applied)
        self.assertTrue(m.fade(r.memory_id).applied)
        self.cycle.run(replace(self.due(), payload={'owner_ref':'int_orphan'}))
        self.assertNotIn('Лидия', json.dumps(self.provider.calls[-1], ensure_ascii=False))

    def test_malformed_model_intentions_are_excluded_without_cycle_crash(self):
        from iam.domain import Op
        from dataclasses import replace
        self.build(Reply(proposals=(rest(),)))
        m = PersistentMemory(self.c)
        self.cycle.memory = m
        r = m.prepare(kind='episodic',origin='own_hypothesis',text='доступный опыт',sources=(self.root(self.c),))
        self.assertTrue(m.write(r).applied)
        for value in ({'text':'случай','sources':[123]},
                      {'text':'случай','sources':[r.memory_id],'source_versions':'oops'},
                      {'text':123,'sources':[r.memory_id]},
                      {'text':'случай','sources':[]},
                      {'text':'случай','sources':[r.memory_id],'source_versions':{r.memory_id:True}}):
            with self.subTest(value=value):
                self.assertTrue(self.c.apply(self.prop(self.c, Op('intentions','int_broken','set',value,True),actor='model')).applied)
                self.assertEqual(m.decision_state()['intentions'], {})
                self.provider.queue(Reply(proposals=(rest(),)))
                result = self.cycle.run(replace(self.due(),payload={'owner_ref':'int_broken'}))
                self.assertEqual(result.outcome, 'done')
