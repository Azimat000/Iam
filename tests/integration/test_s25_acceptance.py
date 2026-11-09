import tempfile
import unittest
from pathlib import Path

from iam.storage import Store

from integration.intention_probe import snapshot
from integration.project_affordance import events, next_session
from integration.s25_acceptance import CONTROLS, initial_record, prepare_controls, prepare_primary


class AcceptanceSetupTest(unittest.TestCase):
    def test_independent_initial_histories_have_no_works_or_goals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initial = prepare_primary(root)
            self.assertEqual(len(initial), 5)
            identities = set()
            for branch in initial:
                with Store.open(root / f'initial/{branch}.db', readonly=True) as store:
                    state = store.latest_snapshot().state
                    self.assertFalse(state['intentions'])
                    self.assertFalse(state['world_time']['world'].get('works'))
                    self.assertFalse(state['world_time']['world'].get('library'))
                    identities.add(store.persona_id)
            self.assertEqual(len(identities), 5)

    def test_control_copies_start_with_same_full_history_and_separate_switches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = root / 'primary'
            prepare_primary(primary)
            summary = {'sessions': []}
            for story, _ in CONTROLS:
                checkpoint = primary / f'checkpoints/{story}.db'
                snapshot(primary / f'initial/{story}.db', checkpoint)
                item = initial_record(checkpoint)
                summary['sessions'].append(dict(branch=story, number=2, head=item['head'],
                                                checkpoint_sha256=item['sha256']))
            controls = root / 'controls'
            initial, switches, hashes, missing = prepare_controls(controls, primary, summary)
            self.assertEqual(len(initial), 12)
            self.assertEqual(len(hashes), 3)
            self.assertEqual(missing, [])
            for branch in initial:
                story, _, mode = branch.split('-')
                with (Store.open(controls / f'initial/{branch}.db', readonly=True) as copy,
                      Store.open(primary / f'checkpoints/{story}.db', readonly=True) as origin):
                    self.assertEqual(events(copy), events(origin))
                    self.assertEqual(copy.latest_snapshot().state, origin.latest_snapshot().state)
                self.assertEqual(mode, switches[branch])
            first, second = list(initial)[:2]
            self.assertEqual(next_session([dict(branch=first, number=1, status='completed')],
                                          list(initial), 1), (second, 1))
