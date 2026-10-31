"""Мотивация не выбирает за A; результаты, отключения и основания проверяемы."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import experiments.test_development as support
from iam.experiments.conversation import CAP_DIALOGUE, records
from iam.experiments.development import CAP_DEVELOPMENT, DevelopmentRuntime
from iam.experiments.transfer import POLICIES, TransferArchitecture, TransferCycle
from iam.state import verify
from iam.stubs import Reply, ScriptedProvider


class TransferTests(unittest.TestCase):
    setUp = support.DevelopmentCase.setUp
    decide = support.DevelopmentCase.decide
    incoming = support.DevelopmentCase.incoming
    reopen = support.DevelopmentCase.reopen

    def open_cycle(self, calendar=0):
        self.runtime = DevelopmentRuntime(self.store, self.persona, self.switches, calendar_ms=calendar)
        self.runtime.step({'kind': 'session', 'action': 'start'})
        self.provider = ScriptedProvider()
        self.cycle = TransferArchitecture(lambda _: self.provider).build(self.runtime)

    def signals(self):
        return self.cycle.signals(records(self.runtime.controller))

    def write(self, text='Она оставила ключ на столе.'):
        return self.decide(support.operation('write_project', 'draft', title='Сцена', topic='creative_work', text=text))

    def test_no_reward_for_version_count_and_no_scripted_goal(self):
        self.write()
        self.write()
        signal = self.signals()['items'][0]
        self.assertTrue(signal['last_two_visible_texts_identical'])
        self.assertEqual(signal['inputs']['novelty'], 0)
        self.assertEqual(signal['inputs']['strength'], 0)
        self.assertEqual(signal['inputs']['score'], 0)
        self.assertEqual(signal['accepted_goals'], [])
        self.assertFalse(self.runtime.controller.state['intentions'])
        self.write('Она забрала ключ и ушла.')
        self.assertFalse(self.signals()['items'][0]['last_two_visible_texts_identical'])
        self.assertFalse(self.signals()['meaningful_progress_measured'])
        self.assertFalse(verify(self.store))

    def test_model_can_choose_any_action_despite_salience(self):
        self.write()
        for mode in POLICIES:
            self.cycle.mechanism = POLICIES[mode]()
            captured = []
            report = self.decide(lambda req: (captured.append(req), support.operation('rest')(req))[1])
            self.assertEqual(report.actions[0].status, 'confirmed')
            self.assertEqual(len(self.cycle.world_state['projects']['draft']['versions']), 1)
            signal = json.loads(captured[0]['context'][-1]['text'])['motivation']
            self.assertEqual(signal['mechanism'], mode)
            self.assertFalse(signal['selects_action'])
        self.assertEqual(self.decide(support.silence).outcome, 'done')

    def test_goal_bonus_requires_accepted_grounded_active_intention(self):
        self.write()
        base = self.signals()['items'][0]['salience']
        self.decide(support.intention({'op':'create','id':'finish','text':'Добавить в сцену последствие ухода.', 'project':'draft'}))
        signal = self.signals()['items'][0]
        self.assertEqual(signal['accepted_goals'], ['finish'])
        self.assertAlmostEqual(signal['salience'] - base, 0.45)
        self.decide(support.intention({'op':'transition','id':'finish','to_status':'deferred'}))
        self.assertEqual(self.signals()['items'][0]['accepted_goals'], [])
        self.assertEqual(self.signals()['items'][0]['salience'], base)

    def test_unknown_and_rejected_actions_do_not_become_experience(self):
        self.runtime.world.will(CAP_DEVELOPMENT, 'unknown')
        self.assertEqual(self.write().actions[0].status, 'unknown_outcome')
        self.assertEqual(self.signals()['items'], [])
        self.assertEqual(self.decide(support.operation('read_material', 'missing')).outcome, 'rejected')
        self.assertEqual(self.signals()['items'], [])

    def test_failure_survives_reopen_without_fabricating_file_or_result(self):
        self.write()
        self.runtime.world.will(CAP_DEVELOPMENT, 'fail')
        report = self.write('Другой текст, который не удалось сохранить.')
        self.assertEqual(report.actions[0].status, 'failed')
        self.assertEqual(len(self.cycle.world_state['projects']['draft']['versions']), 1)
        self.assertEqual(len(list((self.root/'artifacts').glob('*.txt'))), 1)
        before = self.signals()['items'][0]
        self.assertEqual(before['inputs']['failures'], 1)
        self.assertEqual(before['inputs']['score'], -0.25)
        self.assertEqual(before['inputs']['strength'], 0)
        self.reopen()
        self.assertEqual(self.signals()['items'], [before])
        captured = []
        self.decide(lambda req: (captured.append(req), support.silence(req))[1])
        self.assertIn('Подтверждённая неудача', json.dumps(captured[0],ensure_ascii=False))
        self.write('После неудачи удалось сохранить новую сцену.')
        self.assertEqual(self.signals()['items'][0]['inputs']['failures'], 0)
        self.assertFalse(verify(self.store))

    def test_disabled_motivation_or_memory_and_archive_do_not_return_signals(self):
        self.write('PRIVATE-DRAFT')
        recalled = records(self.runtime.controller)
        self.runtime.switches['motivation_off'] = True
        self.assertEqual(self.signals(), {'enabled': False})
        self.runtime.switches['motivation_off'] = False
        self.cycle.memory.off = True
        self.assertEqual(self.signals(), {'enabled': False})
        self.runtime.world.will(CAP_DEVELOPMENT, 'fail')
        count = len(records(self.runtime.controller))
        self.write()
        self.assertEqual(len(records(self.runtime.controller)), count)
        self.cycle.memory.off = False
        archived = replace(recalled[0],layer='archive')
        self.assertEqual(self.cycle.signals([archived])['items'], [])
        captured=[]
        original = self.store.get_memory
        with patch.object(self.store,'get_memory',side_effect=lambda mid: archived if mid==archived.memory_id else original(mid)):
            self.decide(lambda req: (captured.append(req), support.silence(req))[1])
        self.assertEqual(json.loads(captured[0]['context'][-1]['text'])['motivation']['items'], [])
        self.assertNotIn('PRIVATE-DRAFT',json.dumps(captured[0],ensure_ascii=False))

    def test_only_visible_grounded_records_and_unique_outcomes_count(self):
        self.write()
        original = records(self.runtime.controller)[0]
        self.assertEqual(self.cycle.signals([original,original]),self.cycle.signals([original]))
        self.assertEqual(self.cycle.signals([])['items'], [])
        for rec in (replace(original,sources=()), replace(original,origin='own_hypothesis'),
                    replace(original,sources=('evt_MissingSource01',))):
            self.assertEqual(self.cycle.signals([rec])['items'], [])
        self.incoming('UNREAD-PRIVATE')
        self.decide(support.operation('read_message','first'))
        self.assertEqual(len(self.signals()['items']), 1)

    def test_materials_rest_and_messages_do_not_add_writing_progress(self):
        self.decide(support.operation('read_material','craft'))
        self.decide(support.operation('rest'))
        self.assertEqual([x['activity'] for x in self.signals()['items']], ['material:craft'])
        self.decide(support.speech)
        self.assertEqual(len(self.signals()['items']), 1)
        self.runtime.world.will(CAP_DIALOGUE,'fail')
        self.incoming('Ответь, если хочешь.')
        self.decide(support.operation('read_message','first'))
        self.decide(support.speech)
        self.assertFalse(any('Подтверждённая неудача' in r.text for r in records(self.runtime.controller)))

    def test_own_interpretation_is_used_only_when_visible_and_never_learned_by_formula(self):
        self.decide(support.operation('read_material','craft'))
        self.decide(lambda req: Reply(proposals=(support.appraisal(req),)))
        self.assertEqual(self.signals()['items'][0]['inputs']['strength'],0.2)
        self.write()
        self.assertEqual(self.signals()['items'][-1]['inputs']['strength'],0.2)
        self.runtime.switches['interests_off']=True
        self.assertEqual(self.signals()['items'][-1]['inputs']['strength'],0)

    def test_mode_and_retry_validation(self):
        with self.assertRaises(ValueError):
            TransferArchitecture(lambda _: self.provider,'missing')
        with self.assertRaises(ValueError):
            TransferCycle(self.runtime,self.provider,'missing')
        with self.assertRaises(ValueError):
            TransferArchitecture(lambda _: SimpleNamespace(max_format_retries=1)).build(self.runtime)

    def test_failure_without_prior_success_and_non_activity_source(self):
        self.runtime.world.will(CAP_DEVELOPMENT,'fail')
        self.write()
        self.assertEqual(self.signals()['items'][0]['inputs']['failures'],1)
        self.assertEqual(self.signals()['items'][0]['inputs']['repeats'],0)
        self.write()
        self.decide(support.speech)
        events=self.runtime.controller.events_after(0)
        sent=next(e for e in events if e.type=='message.out_sent')
        original=records(self.runtime.controller)[-1]
        fake=SimpleNamespace(type='action.confirmed',payload=sent.payload)
        with patch.object(self.runtime.controller,'find_event',return_value=fake):
            self.assertEqual(self.cycle.signals([original])['items'],[])
        from iam.experiments.agency import AgencyCycle
        action=SimpleNamespace(status='failed',events=[sent.event_id],action_id=sent.payload['action_id'])
        count=len(records(self.runtime.controller))
        with patch.object(AgencyCycle,'_verified'):
            self.cycle._verified(SimpleNamespace(actions=[action]),{},None)
        self.assertEqual(len(records(self.runtime.controller)),count)
