"""Journal verification rejects added memories and does not retain historical bodies."""
import weakref
from unittest.mock import patch

from iam.domain import MemoryRecord
from iam.state import verify
from state.helpers import StateCase, memory, set_


class ReplayIntegrityTest(StateCase):
    def test_extra_memory_without_transition_is_detected(self):
        c = self.controller(running=True)
        self.assertEqual(verify(c.store), [])
        c.store.add_memory(MemoryRecord.from_dict(memory('Не записанный в журнале опыт')))
        self.assertEqual(verify(c.store), ['запись памяти mem_AAAAAAAA в хранилище отсутствует в журнале'])

    def test_extra_memory_is_detected_even_without_any_transition(self):
        c = self.controller(created=False)
        c.store.add_memory(MemoryRecord.from_dict(memory('Нет биографии')))
        self.assertEqual(verify(c.store), ['запись памяти mem_AAAAAAAA в хранилище отсутствует в журнале'])

    def test_verification_releases_old_snapshot_bodies(self):
        c = self.controller(running=True)
        for n in range(10):
            self.assertTrue(c.apply(self.prop(c, set_('beliefs', f'fact_{n}', 'данные' * 100))).applied)
        original = c.store.get_snapshot
        refs, counts = [], []
        def fetch(*args):
            snap = original(*args)
            refs.append(weakref.ref(snap))
            counts.append(sum(ref() is not None for ref in refs))
            return snap
        with patch.object(c.store, 'get_snapshot', side_effect=fetch):
            self.assertEqual(verify(c.store), [])
        self.assertGreater(len(refs), 10)
        self.assertLessEqual(max(counts), 2)
        self.assertTrue(all(ref() is None for ref in refs))
