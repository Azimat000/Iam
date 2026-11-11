"""Обычная сборка панели живёт без внешнего ввода и ручного назначения поводов."""
from unittest.mock import patch

from iam.backup import create_backup, restore_backup
from iam.clock import life_moment
from iam.cycle import Runner
from iam.domain import new_id
from iam.interfaces import ActionOutcome
from iam.panel.runtime import Runtime
from iam.state import StateController, verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, attend, rest
from iam.world import READ, WRITE
from iam.world.autonomy import AutonomousRunner, OWNER, START_MS, STEP_MS, REST_MS
from state.helpers import StateCase


class AutonomyTest(StateCase):
    def runtime(self, path=None):
        path = path or self.db
        if not path.exists():
            with Store.create(path, new_id('exp')) as store:
                StateController(store).create_persona()
        provider = ScriptedProvider()
        provider.max_format_retries = 1
        # Начальный тестовый Runtime; при reopen собственное время восстанавливаем из базы.
        def controller(store):
            from iam.clock import LifeClock
            return StateController(store, clock=LifeClock.recover(store, monotonic=self.time.mono,
                                                                 calendar=self.time.calendar))
        runtime = Runtime(path)
        with patch('iam.panel.runtime.CodexProvider', return_value=provider), patch('iam.panel.runtime.StateController', side_effect=controller):
            runtime._setup()
        original = provider.propose
        provider.propose = lambda request: (runtime.session.charge(), original(request))[1]
        self.addCleanup(runtime.store.close)
        return runtime, provider

    def setUp(self):
        super().setUp()
        self.rt, self.provider = self.runtime()
        self.material = self.rt.cycle.world.add_material('Окно', 'Свет падает на пустой стол.', 'Изолированный тест')
        self.rt.command('conversation', 'Изолированный тест без входящих')

    def step(self, ms=0):
        self.time.advance(ms)
        return self.rt.session.step(self.rt.runner)

    def choices(self, *proposals):
        pending = iter(proposals)
        def reply(request):
            return Reply((next(pending),)) if request['purpose'] == 'choose_action' else Reply()
        self.provider.queue(*([reply] * (3 * len(proposals))))

    def moments(self):
        return [m for m in self.rt.ctl.state['world_time']['moments'].values() if m['owner_ref'].startswith(OWNER)]

    def test_normal_non_demo_runtime_reads_writes_rests_and_returns_without_mail(self):
        self.choices(act(READ, {'material_id':self.material}),
                     act(WRITE, {'project_id':'light','title':'Свет','text':'На столе остался след света.'}),
                     rest('Побуду в тишине'), rest('Пока ещё отдохну'))
        self.assertEqual(self.step(), [])
        self.assertEqual(len(self.moments()), 1)
        self.assertEqual(len(self.provider.calls), 0)
        self.assertEqual(self.step(START_MS)[0].actions[0].status, 'confirmed')
        self.assertTrue(self.rt.cycle.world.state['library'][self.material]['read'])
        self.assertEqual(self.step(STEP_MS)[0].actions[0].status, 'confirmed')
        self.assertIn('light', self.rt.cycle.world.state['projects'])
        self.assertEqual(self.step(STEP_MS)[0].outcome, 'done')
        calls = len(self.provider.calls)
        self.assertEqual(self.step(REST_MS - 1), [])
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(self.step(1)[0].outcome, 'done')
        self.assertFalse(any(e.type == 'input.arrived' for e in self.rt.store.events()))
        self.assertEqual(verify(self.rt.store), [])

    def test_pause_restart_and_restore_preserve_rest_and_do_not_repeat_work(self):
        self.choices(rest())
        self.step(START_MS)
        self.step(1000)
        before = self.moments()
        self.rt.command('pause', 'Проверяем паузу')
        self.assertEqual(self.step(86_400_000), [])
        self.assertEqual(self.moments(), before)
        backup = create_backup(self.db, self.tmp/'backups')
        restored = restore_backup(backup.info.path, self.tmp/'restored', reason='Проверка восстановления')
        self.rt.store.close()
        self.rt, self.provider = self.runtime()
        self.assertEqual(self.moments(), before)
        self.rt.command('resume', 'Продолжаем тот же сеанс')
        self.choices(rest())
        self.assertEqual(self.step(REST_MS - 1001), [])
        self.assertEqual(self.step(1)[0].outcome, 'done')
        with Store.open(restored.path) as store:
            ctl = StateController(store)
            ctl.resume_session()
            clone = AutonomousRunner(self.rt.cycle, ctl, self.rt.cycle.creator)
            self.assertEqual(clone.plan()['moment_id'], before[0]['moment_id'])
            clone.reconcile()
            self.assertEqual(list(ctl.state['world_time']['moments'].values()), before)

    def test_crash_after_result_before_plan_recovers_same_future_moment(self):
        self.choices(act(READ, {'material_id':self.material}))
        self.rt.runner.reconcile()
        self.time.advance(START_MS)
        # Обрыв между выполненным обычным шагом и reconcile, а не ручной повод.
        Runner.run_pending(self.rt.runner)
        expected = self.rt.runner.plan()
        self.assertEqual(self.moments(), [])
        self.rt.ctl.clock.heartbeat(self.rt.store)
        self.rt.store.close()
        self.rt, self.provider = self.runtime()
        self.assertEqual(self.rt.ctl.session_state, 'paused')
        self.rt.command('resume', 'После обрыва')
        self.assertEqual(self.step(), [])
        self.assertEqual(self.moments(), [expected])
        self.choices(rest())
        self.step(STEP_MS)
        self.assertEqual(sum(e.type == 'action.confirmed' for e in self.rt.store.events()), 1)

    def test_due_saved_before_restart_is_consumed_once(self):
        self.rt.runner.reconcile()
        self.time.advance(START_MS)
        self.rt.ctl.tick()
        self.rt.command('pause', 'После наступления момента, до выбора')
        self.rt.store.close()
        self.rt, self.provider = self.runtime()
        self.rt.command('resume', 'Продолжаем')
        self.choices(rest())
        self.assertEqual(len(self.step()), 1)
        for _ in range(10): self.assertEqual(self.step(), [])
        self.assertEqual(len(self.provider.calls), 1)

    def test_superseded_backlog_and_other_owners_are_not_replayed_or_cancelled(self):
        self.rt.runner.reconcile()
        self.time.advance(START_MS)
        due = self.rt.ctl.tick()[0].events[0]
        other = life_moment(new_id('mom'), self.rt.ctl.life_time_ms + 12345, 'my_intention')
        self.rt.ctl.schedule_moment(other)
        self.choices(rest())
        # Выбор из другого повода сделал уже накопившийся служебный момент устаревшим.
        self.rt.cycle.run(self.rt.store.get_event(1))
        self.rt.runner.backlog = [(due, 0)]
        calls = len(self.provider.calls)
        self.assertEqual(self.step(), [])
        self.assertEqual(len(self.provider.calls), calls)
        self.assertIn(other['moment_id'], self.rt.ctl.state['world_time']['moments'])
        self.assertFalse(self.rt.runner.is_current(due))
        self.rt.command('stop', 'Новый сеанс')
        self.rt.command('start', 'Ограниченный сеанс', max_calls=1)
        self.step()
        self.assertEqual(len(self.moments()), 1)
        self.assertEqual(self.moments()[0]['due_life_ms'], self.rt.ctl.life_time_ms + START_MS)

    def test_call_limit_and_stopped_session_forbid_autonomous_work(self):
        self.rt.command('stop', 'Перейти к проверочному бюджету')
        self.rt.command('start', 'Один вызов', max_calls=1)
        self.choices(rest())
        self.step(START_MS)
        self.assertEqual(self.rt.ctl.session_state, 'paused')
        self.assertEqual(self.rt.session.usage()['calls'], 1)
        for _ in range(3): self.assertEqual(self.step(REST_MS), [])
        self.assertEqual(len(self.provider.calls), 1)

    def test_no_accepted_decision_does_not_create_endless_new_attempts(self):
        self.provider.queue(*([Reply()] * 5))
        self.step(START_MS)
        for _ in range(10): self.step(REST_MS)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.moments(), [])

    def test_pending_action_is_not_a_result_and_failed_result_allows_new_choice(self):
        self.choices(act(READ, {'material_id':self.material}))
        original = self.rt.cycle._act
        def before_execution(*args):
            self.assertIsNone(self.rt.runner.plan())
            self.rt.runner.reconcile()
            return original(*args)
        with patch.object(self.rt.cycle, '_act', side_effect=before_execution), patch.object(
                self.rt.cycle.world, 'execute', return_value=ActionOutcome('failed', 'Не удалось прочесть')):
            self.step(START_MS)
        self.assertEqual(self.rt.runner.plan()['due_life_ms'], self.rt.ctl.life_time_ms + STEP_MS)
        self.assertEqual(len(self.moments()), 1)

    def test_without_session_and_rest_attention(self):
        self.rt.command('stop', 'Проверка отсутствия сеанса')
        self.assertIsNone(self.rt.runner.plan())
        self.rt.command('start', 'Отдых через внимание')
        self.choices(attend('rest'))
        self.step(START_MS)
        self.assertEqual(self.moments()[0]['due_life_ms'], self.rt.ctl.life_time_ms + REST_MS)

    def test_crash_before_attempt_does_not_replay_action_and_resume_allows_new_choice(self):
        self.choices(act(READ, {'material_id':self.material}))
        with patch.object(self.rt.cycle, '_act', side_effect=RuntimeError('crash before attempt')):
            self.step(START_MS)
        self.assertIsNone(self.rt.runner.plan())
        self.assertEqual(sum(e.type == 'action.attempted' for e in self.rt.store.events()), 0)
        self.rt.store.close()
        self.rt, self.provider = self.runtime()
        self.rt.command('resume', 'После обрыва до исполнения')
        self.choices(rest())
        self.assertEqual(self.step(), [])
        self.assertEqual(self.step(START_MS)[0].outcome, 'done')
        self.assertEqual(sum(e.type == 'action.attempted' for e in self.rt.store.events()), 0)
        self.assertFalse(self.rt.cycle.world.state['library'][self.material]['read'])
        self.assertEqual(sum(e.payload.get('fault') == 'unstarted_action' for e in self.rt.store.events()), 1)
        self.assertIn('не было начато', str(self.provider.calls[-1]['context']))
        self.assertEqual(verify(self.rt.store), [])

    def test_crash_after_attempt_is_unknown_not_unstarted(self):
        self.choices(act(READ, {'material_id':self.material}))
        with patch.object(self.rt.cycle.world, 'execute', side_effect=SystemExit('process died')):
            with self.assertRaises(SystemExit): self.step(START_MS)
        self.rt.store.close()
        self.rt, self.provider = self.runtime()
        self.rt.command('resume', 'После обрыва во время действия')
        self.assertIsNone(self.rt.runner.plan())
        self.step()
        self.assertEqual(sum(e.type == 'action.unknown_outcome' for e in self.rt.store.events()), 1)
        self.assertFalse(any(e.payload.get('fault') == 'unstarted_action' for e in self.rt.store.events()))
        self.assertEqual(len(self.moments()), 1)

    def test_appraisal_acceptance_alone_does_not_advance_choice_anchor(self):
        self.choices(act(READ, {'material_id':self.material}))
        self.step(START_MS)
        plan = self.rt.runner.plan()
        row = self.rt.cycle.memory.records()[0]
        request = self.rt.cycle._request(new_id('cyc'),
                    [{'ref':row.memory_id,'origin':row.origin,'layer':row.layer,'text':row.text}],
                    0, None, purpose='appraise')
        request['allowed_proposals'] = ['memory_write']
        self.rt.ctl.record_request(request)
        proposal = {'proposal_id':new_id('prop'),'kind':'memory_write','rationale':'Мысль после чтения',
                    'cites':[row.memory_id],'body':{'draft':{'kind':'semantic','origin':'own_hypothesis',
                    'content':{'text':'Мне запомнился свет'}}}}
        # Обычная память без социальной/эмоциональной трактовки допустима в этом техническом appraise.
        self.rt.cycle._appraisal_sources = frozenset([row.memory_id])
        answer = ScriptedProvider([Reply((proposal,))]).propose(request)
        self.assertTrue(self.rt.ctl.receive_response(answer).status in ('applied','partial'))
        self.assertEqual(self.rt.runner.plan(), plan)
