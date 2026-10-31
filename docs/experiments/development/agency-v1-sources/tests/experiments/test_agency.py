"""Выбор остаётся свободным, осмысление добровольно, история не подменяется декларацией."""
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import experiments.test_development as support
from iam.domain import new_id
from iam.experiments import Persona
from iam.experiments.agency import AgencyArchitecture, CONTRACT, PERSONAL
from iam.experiments.development import DevelopmentRuntime
from iam.experiments.report.__main__ import main
from iam.experiments.report.development import run_development, save_trace
from iam.state import verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider

ROOT = Path(__file__).resolve().parents[2]


class AgencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.persona = Persona.load(ROOT / 'scenarios/personas/curious.toml')
        self.store = Store.create(self.root / 'persona.db', new_id('exp'))
        self.addCleanup(lambda: self.store.close())
        self.switches = dict(support.SWITCHES)
        self.runtime = DevelopmentRuntime(self.store, self.persona, self.switches)
        self.runtime.step({'kind': 'session', 'action': 'start'})
        self.provider = ScriptedProvider()
        self.architecture = AgencyArchitecture(lambda _: self.provider)
        self.cycle = self.architecture.build(self.runtime)
        self.step = 0

    def decide(self, answer):
        self.provider.queue(answer)
        self.step += 1
        self.runtime.step({'kind': 'wait', 'life_ms': 60000})
        return self.cycle.run(self.runtime.trigger('agency_' + str(self.step)))

    def write(self, text):
        return self.decide(support.operation('write_project', 'notes', title='Моя сцена', topic='creative_work', text=text))

    def test_successful_work_does_not_force_appraisal_interest_or_intention(self):
        report = self.decide(support.operation('read_material', 'craft'))
        self.assertEqual(report.outcome, 'done')
        self.assertEqual(len(report.requests), 1)
        self.assertIsNone(self.cycle.appraisal_provider)
        self.assertFalse(self.runtime.controller.state['intentions'])
        self.assertFalse(self.cycle.view()['interests'])
        self.assertEqual(self.decide(support.operation('rest')).outcome, 'done')

    def test_voluntary_grounded_appraisal_can_change_later_visible_interest(self):
        self.decide(support.operation('read_material', 'craft'))
        report = self.decide(lambda req: Reply(proposals=(support.appraisal(req),
                                      *support.operation('rest')(req).proposals)))
        self.assertEqual(report.outcome, 'done')
        self.assertEqual(self.cycle.view()['interests']['creative_work']['values']['strength'], .2)
        self.assertFalse(verify(self.store))

    def test_continuity_distinguishes_first_exact_repeat_and_changed_work_across_reopen(self):
        self.write('Сцена: она оставила ключ на столе.')
        self.assertIsNone(self.cycle.view()['work_continuity']['notes']['last_two_texts_identical'])
        self.write('Сцена: она оставила ключ на столе.')
        self.assertTrue(self.cycle.view()['work_continuity']['notes']['last_two_texts_identical'])
        self.write('Сцена: она забрала ключ и вышла.')
        self.assertFalse(self.cycle.view()['work_continuity']['notes']['last_two_texts_identical'])
        self.runtime.step({'kind': 'session', 'action': 'stop'})
        reopened = DevelopmentRuntime(self.store, self.persona, self.switches, calendar_ms=86400000)
        reopened.step({'kind': 'session', 'action': 'start'})
        cycle = self.architecture.build(reopened)
        self.assertEqual(cycle.view()['work_continuity']['notes']['versions'], 3)
        self.assertFalse(cycle.view()['work_continuity']['notes']['last_two_texts_identical'])

    def test_memory_off_does_not_expose_new_comparison_or_read_unread_message(self):
        self.write('PRIVATE-CONTENT')
        self.runtime.step({'kind': 'input', 'id': 'unread', 'sender': 'operator', 'text': 'UNREAD-SECRET'})
        self.cycle.memory.off = True
        captured = []
        def answer(req):
            captured.append(req)
            return support.operation('rest')(req)
        self.decide(answer)
        text = json.dumps(captured[0], ensure_ascii=False)
        self.assertNotIn('work_continuity', json.dumps(self.cycle.view()))
        self.assertNotIn('PRIVATE-CONTENT', text)
        self.assertNotIn('UNREAD-SECRET', text)

    def test_context_keeps_contract_and_sources_without_forced_appraisal_request(self):
        captured = []
        self.decide(lambda req: (captured.append(req), support.operation('rest')(req))[1])
        context = captured[0]['context']
        self.assertEqual(context[0]['text'], PERSONAL + CONTRACT)
        self.assertIn('result_ref', CONTRACT)
        self.assertIn('source_ref', CONTRACT)
        self.assertIn('experience_sources', json.loads(context[-2]['text']))
        self.assertEqual(captured[0]['purpose'], 'choose_action')

    def test_provider_retries_are_rejected_without_changing_model(self):
        with self.assertRaises(ValueError):
            AgencyArchitecture(lambda _: SimpleNamespace(max_format_retries=1)).build(self.runtime)

    def test_new_trace_protocol_and_intention_validation_remain_explicit(self):
        self.write('Подтверждённый текст работы.')
        self.runtime.step({'kind': 'session', 'action': 'stop'})
        result, problems = save_trace(self.root, self.architecture, self.persona, [], [], self.switches)
        self.assertEqual(result.trace['scenario'], 'agency_v1')
        self.assertFalse(problems)
        verifier = runpy.run_path(str(ROOT / 'scripts/experiments/verify_development.py'))
        import hashlib
        trace, _, artifacts = verifier['verify_trace'](self.root / 'trace.json', hashlib.sha256(result.trace_bytes).hexdigest())
        self.assertEqual(trace['architecture'], 'A-agency-v1')
        self.assertTrue(artifacts)

    def test_profile_validates_before_creating_output_and_default_stays_legacy(self):
        for kwargs in ({'profile':'unknown'}, {'profile':'agency-v1'},
                       {'profile':'agency-v1', 'variants':('A',), 'mode':'formal'}):
            with self.assertRaises(ValueError):
                run_development(ROOT, self.root / 'invalid', **kwargs)
        self.assertFalse((self.root / 'invalid').exists())
        summary = run_development(ROOT, self.root / 'stub', profile='agency-v1', variants=('A',), repeats=1)
        self.assertEqual(summary['mechanism'], 'agency-v1')
        self.assertEqual(summary['executed'], 2)
        self.assertEqual(summary['human_packet'], 0)
        self.assertIsNone(summary['human_ratings'])
        self.assertTrue(all(row['calls'] <= 26 for row in summary['runs']))

    def test_cli_profile_is_explicit_A_only_and_keeps_legacy_kwargs(self):
        for extra in ([], ['--life'], ['--development','--mode','formal'], ['--development','--variants','B']):
            with self.assertRaises(SystemExit):
                main(['--output',str(self.root/'out'),'--development-profile','agency-v1',*extra])
        with patch('iam.experiments.report.__main__.run_development', return_value={'cells':2,'executed':2,'human_packet':0}) as runner:
            self.assertEqual(main(['--output',str(self.root/'out'),'--development','--development-profile','agency-v1']),0)
            self.assertEqual(runner.call_args.kwargs['profile'],'agency-v1')
            self.assertEqual(runner.call_args.kwargs['variants'],('A',))
