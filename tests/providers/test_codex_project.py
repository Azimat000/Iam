"""Project text travels as native fields and still requires a normal core action."""
import copy
import json
import unittest

from iam.providers.codex.provider import _proposals
from iam.stubs import act
from iam.world import WRITE
from world.helpers import LivingCase


class CodexProjectWireTest(unittest.TestCase):
    def packet(self, **extra):
        return {'attention': None, 'explanation': '', 'proposals': [
            {'kind': 'project_write', 'rationale': 'Сохранить замер', 'cites': [],
             'project_id': 'shoreline_evidence_note', 'title': 'Берег',
             'text': '«Отметка» {11 см}.\nЗапись со скобками: }.', **extra},
        ]}

    def test_native_write_and_old_valid_action_normalize_identically(self):
        data = self.packet()
        native = data['proposals'][0]
        params = {key: native[key] for key in ('project_id', 'title', 'text')}
        legacy = {**data, 'proposals': [
            {'kind': 'action', 'rationale': native['rationale'], 'cites': [],
             'body_json': json.dumps({'capability_id': WRITE, 'params': params})},
        ]}
        new, = _proposals(data)
        old, = _proposals(legacy)
        self.assertEqual(new['body'], {'capability_id': WRITE, 'params': params})
        self.assertEqual({k: v for k, v in new.items() if k != 'proposal_id'},
                         {k: v for k, v in old.items() if k != 'proposal_id'})

    def test_reject_missing_field_extra_override_and_incomplete_legacy(self):
        missing = self.packet()
        del missing['proposals'][0]['text']
        malformed = {'attention': None, 'explanation': '', 'proposals': [
            {'kind': 'action', 'rationale': 'Сохранить', 'cites': [],
             'body_json': '{"capability_id":"cap_Project01","params":{"project_id":"shoreline_evidence_note",'
                          '"title":"Берег","text":"Запись"}'},
        ]}
        for data in (missing, self.packet(capability_id='cap_Library01'),
                     self.packet(body_json='{}'), self.packet(text=1), malformed):
            with self.subTest(data=data), self.assertRaises(ValueError):
                _proposals(data)


class CodexProjectCoreTest(LivingCase):
    def test_native_write_obeys_normal_project_validation(self):
        packet = CodexProjectWireTest().packet()
        native, = _proposals(packet)
        self.assertEqual(self.run_reply(native).actions[0].status, 'confirmed')
        invalid = copy.deepcopy(packet)
        invalid['proposals'][0]['project_id'] = '../not-a-project'
        normalized, = _proposals(invalid)
        self.assertFalse(self.run_reply(normalized).actions)
        self.assertFalse(self.run_reply(act(WRITE, {'project_id': '../not-a-project',
                                                   'title': 'Берег', 'text': 'Запись'})).actions)
