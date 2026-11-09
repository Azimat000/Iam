"""Native attention enum prevents the recurring notice/body_json mismatch before generation."""
import copy
import json
from pathlib import Path
import unittest

from iam.domain import jsonschema
from iam.providers.codex.command import RESPONSE_JSON_SCHEMA
from iam.providers.codex.provider import _proposals


class NativeAttentionTest(unittest.TestCase):
    def wire(self, focus='dialogue', ref='inp_Notice0001', cites=()):
        return {'proposals': [], 'explanation': 'Attend to a visible notice',
                'attention': {'rationale': 'A visible unread notice is available',
                              'cites': list(cites), 'focus': focus, 'ref': ref}}

    def test_old_opaque_envelope_cannot_advertise_invalid_notice_focus(self):
        old = {'proposals': [{'kind': 'attend', 'rationale': 'Visible notice', 'cites': [],
                             'body_json': '{"focus":"notice","ref":"inp_Notice0001"}'}], 'explanation': 'Notice'}
        self.assertTrue(jsonschema.check(RESPONSE_JSON_SCHEMA, old, Path('.')))
        with self.assertRaises(ValueError): _proposals(old)

    def test_typed_attention_copies_exact_fields_and_null_does_not_create_a_proposal(self):
        for focus in ('activity', 'intention', 'event', 'dialogue', 'rest'):
            for ref in ('inp_Notice0001', None):
                with self.subTest(focus=focus, ref=ref):
                    data = self.wire(focus, ref, ['evt_Event0001'])
                    self.assertEqual(jsonschema.check(RESPONSE_JSON_SCHEMA, data, Path('.')), [])
                    proposal, = _proposals(data)
                    self.assertEqual(proposal['kind'], 'attend')
                    self.assertEqual(proposal['body'], {'focus': focus, **({'ref': ref} if ref is not None else {})})
                    self.assertEqual(proposal['cites'], ['evt_Event0001'])
                    self.assertEqual(proposal['rationale'], data['attention']['rationale'])
        data = {'proposals': [], 'explanation': 'No attention choice', 'attention': None}
        self.assertEqual(_proposals(data), [])

    def test_unknown_focus_incomplete_extra_or_forbidden_reference_is_not_repaired(self):
        valid = self.wire()
        variants = [self.wire('notice'), self.wire(cites=['inp_Notice0001']), self.wire(ref=42)]
        for key in valid['attention']:
            bad = copy.deepcopy(valid); del bad['attention'][key]; variants.append(bad)
        bad = copy.deepcopy(valid); bad['attention']['extra'] = 'forbidden'; variants.append(bad)
        bad = copy.deepcopy(valid); bad['proposals'] = [{'kind':'attend','rationale':'duplicate',
             'cites':[],'body_json':'{"focus":"dialogue","ref":"inp_Notice0001"}'}]; variants.append(bad)
        for data in variants:
            with self.subTest(data=data):
                self.assertTrue(jsonschema.check(RESPONSE_JSON_SCHEMA, data, Path('.')))
                with self.assertRaises(ValueError): _proposals(data)

    def test_existing_action_and_attention_preserve_order_and_legacy_reads_remain_available(self):
        data = self.wire()
        data['proposals'] = [{'kind':'action','rationale':'A proposed action','cites':[],
                             'body_json':'{"capability_id":"cap_Project01","params":{"project_id":"draft","title":"Text","text":"A draft"}}'}]
        self.assertEqual([item['kind'] for item in _proposals(data)], ['action', 'attend'])
        legacy = {'proposals': [{'kind':'attend','rationale':'An old valid answer','cites':[],
                                'body_json':'{"focus":"dialogue","ref":"inp_Notice0001"}'}], 'explanation':'History'}
        self.assertEqual(_proposals(legacy)[0]['body']['focus'], 'dialogue')
