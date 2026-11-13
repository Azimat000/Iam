"""Отложенный разговор не теряется при выходе за краткую хронологию."""
import json
from dataclasses import replace
from unittest.mock import patch

from iam.cycle.runner import input_id_for
from iam.social import SocialMemory
from iam.state import EventSpec, verify
from iam.stubs import Reply, StubCreatorBridge, attend, rest
from iam.world import PersistentWorld
from iam.world.options import OptionsCycle
from world.helpers import LivingCase


class DeferredAppraisalTest(LivingCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.inputs = []

    def enable(self):
        self.m = SocialMemory(self.c)
        self.world = PersistentWorld(self.c)
        self.cycle = OptionsCycle(self.c, self.p, self.m, self.world, self.caps,
                                 StubCreatorBridge(), fast_inputs=lambda: True)

    def incoming(self, text=None, *, read=True):
        number = len(self.inputs)
        iid = input_id_for('telegram', str(number))
        arrival = self.c.record_event(EventSpec('input.arrived', 'rx:telegram:'+str(number),
            {'input_id': iid, 'sender': '123', 'channel': 'telegram',
             'text': text or f'Разговор {number}: сегодня я выбрал иной маршрут домой.'})).event
        self.inputs.append(arrival)
        if not read:
            return arrival
        before = len(self.p.calls)
        self.p.queue(Reply((attend('dialogue', iid),)))
        self.assertEqual(self.cycle.run(arrival).outcome, 'done')
        trigger = self.c.find_event(key='rd:'+iid)
        self.p.queue(Reply((rest(),)))
        self.assertEqual(self.cycle.run(trigger).outcome, 'done')
        self.assertEqual([q['purpose'] for q in self.p.calls[before:]], ['choose_action', 'choose_action'])
        return next(row for row in self.m.records() if trigger.event_id in row.sources)

    def idle(self):
        def reply(request):
            return Reply(()) if request['purpose'] in ('appraise', 'generate_options') else Reply((rest(),))
        self.p.queue(reply, reply, reply)
        before = len(self.p.calls)
        report = self.cycle.run(self.due())
        self.p._replies.clear()
        self.assertEqual(report.outcome, 'done', report)
        return self.p.calls[before:]

    def reservations(self):
        return [event.payload['source'] for event in self.c.store.events()
                if event.type == 'proposal.assessed'
                and event.payload.get('phase') == 'experience_appraisal_reserved']

    def test_twelve_fast_reads_are_eventually_reviewed_once_without_assigned_relations(self):
        rows = [self.incoming() for _ in range(12)]
        self.assertEqual(self.reservations(), [])
        for _ in range(12):
            self.idle()
        expected = {row.memory_id for row in rows}
        self.assertEqual(set(self.reservations()), expected)
        self.assertEqual(len(self.reservations()), len(expected))
        self.assertEqual(self.c.state['relations'], {})
        self.assertEqual(self.c.state['beliefs'], {})
        self.assertTrue(expected <= {r.memory_id for r in self.m.records()})
        self.assertEqual(verify(self.c.store), [])

    def test_restart_keeps_reservations_and_continues_older_unseen_reads(self):
        rows = [self.incoming() for _ in range(9)]
        self.idle()
        before = self.reservations()
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        self.enable()
        self.assertTrue(self.c.resume_session().applied)
        for _ in range(9):
            self.idle()
        self.assertEqual(set(self.reservations()), {row.memory_id for row in rows})
        self.assertEqual(len(self.reservations()), len(rows))
        self.assertEqual(self.reservations()[:len(before)], before)
        self.assertEqual(verify(self.c.store), [])

    def test_saturated_interpretations_replace_one_slot_only_for_appraisal(self):
        rows = [self.incoming() for _ in range(8)]
        interpretations = []
        for n in range(self.m.working_limit - 1):
            row = self.m.prepare(kind='semantic', origin='own_hypothesis', text=f'Моя гипотеза {n}',
                                 sources=(rows[-1].memory_id,), layer='interpretation')
            self.assertTrue(self.m.write(row).applied)
            interpretations.append({'ref': row.memory_id, 'origin': row.origin,
                                    'layer': row.layer, 'text': self.m._window(row)})
        with patch.object(self.m.experience, 'context', return_value=interpretations):
            calls = self.idle()
        assessment = next(q for q in calls if q['purpose'] == 'appraise')
        action = next(q for q in calls if q['purpose'] == 'choose_action')
        base = assessment['context'][:self.m.working_limit]
        self.assertEqual(len(base), self.m.working_limit)
        self.assertIn(rows[0].memory_id, {item['ref'] for item in base})
        self.assertTrue(all(len(item['text']) <= self.m.text_limit for item in base))
        self.assertNotIn(rows[0].memory_id, {item['ref'] for item in action['context']})
        self.assertTrue({item['ref'] for item in interpretations} <=
                        {item['ref'] for item in action['context']})

    def test_disabled_forgotten_revised_and_unread_are_not_deferred_sources(self):
        rows = [self.incoming() for _ in range(3)]
        unread = self.incoming('НЕПРОЧИТАННЫЙ СЕКРЕТ', read=False)
        self.assertTrue(self.m.fade(rows[0].memory_id).applied)
        self.assertTrue(self.m.revise(rows[1].memory_id, 'Пересмотренные слова',
                                     sources=(rows[2].memory_id,), reason='Исправление').applied)
        self.m.off = True
        self.idle()
        self.assertEqual(self.reservations(), [])
        self.m.off = False
        self.cycle.motivation.off = True
        self.idle()
        self.assertEqual(self.reservations(), [])
        self.cycle.motivation.off = False
        self.cycle.can_appraise = lambda: False
        self.idle()
        self.assertEqual(self.reservations(), [])
        self.cycle.can_appraise = lambda: True
        self.idle()
        self.assertEqual(self.reservations(), [rows[2].memory_id])
        self.assertNotIn('НЕПРОЧИТАННЫЙ СЕКРЕТ', json.dumps(self.p.calls, ensure_ascii=False))
        self.assertFalse(any(unread.event_id in row.sources for row in self.m.records()))

    def context_slot(self, row):
        return [{'ref': self.root(self.c), 'origin': 'observation', 'layer': 'state', 'text': 'Момент'},
                {'ref': row.memory_id, 'origin': row.origin, 'layer': row.layer, 'text': self.m._window(row)}]

    def narrow_context(self, trigger, focus, records):
        return [self.context_slot(records[0])[0], *[self.context_slot(row)[1] for row in records]]

    def test_small_or_unsafe_oldest_does_not_starve_next_safe_source(self):
        first, second, visible = [self.incoming() for _ in range(3)]
        available = {row.memory_id: row for row in self.m.records()}
        trigger = self.due()
        original = self.m._window
        with patch.object(self.cycle, '_context', side_effect=self.narrow_context):
            for unusable in (None, json.dumps({'text': ''})):
                with patch.object(self.m, '_window', side_effect=lambda r: unusable if r == first else original(r)):
                    chosen = self.cycle._appraisal_context(trigger, None, [visible], available)
                self.assertEqual(chosen[1]['ref'], second.memory_id)
                self.assertEqual(len(chosen), 2)
            safe = self.m._safe_item
            with patch.object(self.m, '_safe_item', side_effect=lambda item: item['ref'] != first.memory_id and safe(item)):
                chosen = self.cycle._appraisal_context(trigger, None, [visible], available)
            self.assertEqual(chosen[1]['ref'], second.memory_id)
            self.m.text_limit = 1
            chosen = self.cycle._appraisal_context(trigger, None, [visible], available)
            self.assertEqual(chosen[1]['ref'], visible.memory_id)
        self.assertEqual(self.reservations(), [])

    def test_no_new_slot_on_input_or_zero_budget_and_invalid_origins_stay_out(self):
        hidden, visible = [self.incoming() for _ in range(2)]
        context = self.context_slot(visible)
        available = {r.memory_id: r for r in self.m.records()}
        trigger = self.due()
        with patch.object(self.cycle, '_context', side_effect=self.narrow_context):
            for incoming_type in ('input.arrived', 'input.read'):
                selected = self.cycle._appraisal_context(replace(trigger, type=incoming_type), None, [visible], available)
                self.assertEqual(selected, context)
            self.cycle.retrieval_limit = 0
            self.assertEqual(self.cycle._appraisal_context(trigger, None, [visible], available), context)
            self.cycle.retrieval_limit = 5
            self.m.working_limit = 1
            self.assertEqual(self.cycle._appraisal_context(trigger, None, [visible], available), context)
            self.m.working_limit = 8
            for invalid in (replace(hidden, origin='own_hypothesis'), replace(hidden, kind='semantic'),
                            replace(hidden, version=2), replace(hidden, content={**hidden.content, 'experience':'prepared'}),
                            replace(hidden, sources=(self.root(self.c),)), replace(hidden, sources=('evt_Absent000',))):
                with self.subTest(invalid=invalid):
                    selected = self.cycle._appraisal_context(trigger, None, [visible], {invalid.memory_id: invalid})
                    self.assertEqual(selected, context)
            self.assertEqual(self.cycle._appraisal_context(trigger, None, [visible], {visible.memory_id: visible}), context)
        # Защита от контекста, который намеренно не предоставляет базовые слоты.
        with patch.object(self.cycle, '_context', return_value=context[:1]):
            self.assertEqual(self.cycle._appraisal_context(trigger, None, [visible], available), context[:1])

    def test_appraisal_keeps_catalog_results_citable_when_old_conversation_is_selected(self):
        rows = [self.incoming() for _ in range(8)]
        self.cycle.can_appraise = lambda: False
        work = self.write('Подтверждённая новая работа о другом предмете')
        self.cycle.can_appraise = lambda: True
        with patch.object(self.m, 'retrieve', return_value=[work]):
            calls = self.idle()
        appraisal = next(q for q in calls if q['purpose'] == 'appraise')
        refs = {item['ref'] for item in appraisal['context']}
        catalog = next(json.loads(item['text']) for item in appraisal['context']
                       if item['text'].startswith('{"library"'))
        self.assertIn(rows[0].memory_id, refs)
        self.assertTrue({item['memory_id'] for item in catalog['world_results']} <= refs)
