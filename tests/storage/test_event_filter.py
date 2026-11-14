"""Отбор до разбора больших аудитов не меняет события, порядок или свежесть чтения."""
from dataclasses import replace
from unittest.mock import patch

from iam.state import EventSpec
from storage.helpers import TmpDirCase, ev
from state.helpers import StateCase


class EventFilterTest(TmpDirCase):
    def test_matches_old_filter_and_never_decodes_unselected_payloads(self):
        store = self.new_store()
        for i, kind in enumerate(('input.arrived', 'proposal.assessed', 'input.read', 'input.arrived'), 1):
            key = {'proposal.assessed': f'assess:req_Filter{i:08d}', 'input.read': f'rd:inp_Filter{i:08d}'}.get(kind)
            event = ev(i, type_=kind, key=key, payload={'text': 'Данные ' * 1000})
            if kind == 'proposal.assessed':
                event = replace(event, changes_state=False)
            store.append_event(event)
        expected = [event for event in store.events() if event.type in ('input.read', 'input.arrived')]
        with patch.object(store, '_row_event', wraps=store._row_event) as decode:
            selected = list(store.events(types=('input.read', 'input.arrived', 'input.arrived')))
            self.assertEqual(decode.call_count, len(expected))
            self.assertTrue(all(call.args[0]['type'] != 'proposal.assessed' for call in decode.call_args_list))
        self.assertEqual(selected, expected)
        self.assertEqual([e.seq for e in store.events(2, 3, types=('input.read', 'input.arrived'))], [3])
        self.assertEqual(list(store.events(5, 4, types=('input.arrived',))), [])
        with patch.object(store, '_row_event', wraps=store._row_event) as decode:
            self.assertEqual(list(store.events(types=())), [])
            self.assertEqual(list(store.events(types=('unknown',))), [])
            self.assertEqual(list(store.events(types=("input.arrived') OR 1=1 --",))), [])
            decode.assert_not_called()
        selected[0].payload['text'] = 'Изменение у получателя'
        self.assertEqual(list(store.events(types=('input.arrived',)))[0], expected[0])

    def test_filter_observes_new_events_and_transaction_rollback(self):
        store = self.new_store()
        store.append_event(ev(1))
        self.assertEqual(len(list(store.events(types=('input.arrived',)))), 1)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with store.transaction():
                store.append_event(ev(2))
                self.assertEqual(len(list(store.events(types=('input.arrived',)))), 2)
                raise RuntimeError('rollback')
        self.assertEqual(len(list(store.events(types=('input.arrived',)))), 1)
        store.append_event(ev(3))
        self.assertEqual([event.payload['n'] for event in store.events(types=('input.arrived',))], [1, 3])


class ControllerEventFilterTest(StateCase):
    def test_controller_preserves_strict_after_and_immediate_visibility_across_pause(self):
        controller = self.controller(running=True)
        before = controller.store.last_seq()
        first = controller.record_event(EventSpec('input.arrived', 'rx:filter:one', {'text': 'Первая'})).event
        self.assertEqual(controller.events_after(before, types=('input.arrived',)), [first])
        self.assertEqual(controller.events_after(first.seq, types=('input.arrived',)), [])
        controller.pause_session('operator')
        second = controller.record_event(EventSpec('input.arrived', 'rx:filter:two', {'text': 'Вторая'})).event
        self.assertEqual(controller.events_after(before, types=('input.arrived',)), [first, second])
        self.assertEqual(controller.events_after(0, types=()), [])
        self.assertEqual([e for e in controller.events_after(before) if e.type == 'input.arrived'], [first, second])
