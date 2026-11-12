import json
from pathlib import Path
import tempfile
import unittest

from iam.clock import LifeClock
from state.helpers import FakeTime
from iam.domain import new_id
from iam.state import StateController
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, rest
from iam.world import READ, WRITE
from iam.world.cycle import PROJECT_CREATION

from integration.intention_probe import snapshot
from integration.live_series import ExperimentCycle
from integration.project_affordance import LegacyGuidanceCycle, next_session
from integration.test_persistent_core import tick, wire


class ProjectAffordanceTest(unittest.TestCase):
    def test_project_key_is_explained_and_remains_usable_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'persona.db'
            with Store.create(path, new_id('exp')) as store:
                ctl = StateController(store)
                ctl.create_persona()
                session, cycle, provider = wire(ctl)
                material = cycle.world.add_material('Образец', 'Наблюдение из библиотеки.', 'Проверка чтения')
                session.command('start', 'Чтение')
                provider.queue(Reply((act(READ, {'material_id': material}),)))
                source = tick(cycle).actions[0].memory_id
                session.command('stop', 'Сохранение')
            control = Path(directory) / 'control.db'
            snapshot(path, control)
            goal = {'kind': 'intention', 'proposal_id': new_id('prop'), 'rationale': 'Вернуться к прочитанному',
                    'cites': [source], 'body': {'op': 'create', 'intention_id': 'notes',
                    'project_id': 'new_notes-2', 'operation': 'write', 'text': 'Подготовить собственную заметку',
                    'criterion': 'В заметке два примера'}}
            requests = []
            time = FakeTime()
            for candidate, cycle_type, proposal in ((path, ExperimentCycle, goal), (control, LegacyGuidanceCycle, rest())):
                def reply(request):
                    requests.append(request)
                    return Reply((proposal,))

                with Store.open(candidate) as store:
                    ctl = StateController(store, clock=LifeClock.recover(
                        store, monotonic=time.mono, calendar=time.calendar))
                    session, cycle, _ = wire(ctl, provider=ScriptedProvider([reply]), cycle_type=cycle_type)
                    session.command('start', 'Выбор')
                    self.assertEqual(tick(cycle).outcome, 'done')
                    session.command('stop', 'Сохранение')
            revised, legacy = requests
            self.assertEqual(revised['allowed_actions'], legacy['allowed_actions'])
            self.assertEqual(revised['allowed_proposals'], legacy['allowed_proposals'])
            revised_text = [item['text'] for item in revised['context']]
            legacy_text = [item['text'] for item in legacy['context']]
            self.assertEqual(sum(PROJECT_CREATION in item for item in revised_text), 1)
            self.assertEqual([item.replace(PROJECT_CREATION, '') for item in revised_text], legacy_text)
            catalog = next(json.loads(item) for item in revised_text if item.startswith('{"library"'))
            self.assertEqual(catalog['projects'], {})
            with Store.open(path) as store:
                ctl = StateController(store)
                session, cycle, provider = wire(ctl)
                self.assertEqual(cycle.intentions.visible()['notes']['project_id'], 'new_notes-2')
                self.assertEqual(cycle.intentions.visible()['notes']['criterion'], 'В заметке два примера')
                session.command('start', 'Возвращение')
                provider.queue(Reply((act(WRITE, {'project_id': 'new_notes-2', 'title': 'Заметка',
                                                 'text': 'Первый и второй примеры.'}),)))
                self.assertEqual(tick(cycle).actions[0].status, 'confirmed')

    def test_worker_schedule_has_no_repeat_after_branch_stop(self):
        self.assertEqual(next_session([]), ('e-library', 1))
        self.assertEqual(next_session([dict(branch='e-library', number=1, status='completed')]), ('e-library', 2))
        self.assertEqual(next_session([dict(branch='e-library', number=1, status='branch_stopped')]), ('independent', 1))
        self.assertIsNone(next_session([dict(branch='e-library', number=1, status='series_stopped')]))
        self.assertIsNone(next_session([dict(branch='e-empty', number=3, status='completed')]))
