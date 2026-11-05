"""Регрессия расхождения wire/shared cites из физического ответа E-memory_off."""
import json
from pathlib import Path
import unittest

from iam.domain import jsonschema
from iam.providers.codex.command import RESPONSE_JSON_SCHEMA
from iam.providers.codex.provider import _proposals


class CitationContractTest(unittest.TestCase):
    def wire(self, cites):
        return {"proposals": [], "attention": {"rationale": "Видно уведомление, его текст не прочитан",
                "cites": cites, "focus": "dialogue", "ref": "inp_Notice0001"},
                "explanation": "Предлагается обратить внимание на уведомление"}

    def test_observed_notice_citation_is_rejected_by_both_schemas(self):
        data = self.wire(["evt_Trigger0001", "inp_Notice0001"])
        with self.assertRaises(ValueError):
            _proposals(data)
        self.assertTrue(jsonschema.check(RESPONSE_JSON_SCHEMA, data, Path('.')))

    def test_notice_remains_valid_attention_target_with_event_or_empty_citation(self):
        for cites in ([], ["evt_Trigger0001"], ["mem_Experience01"], ["evt_Trigger0001", "mem_Experience01"]):
            with self.subTest(cites=cites):
                data = self.wire(cites)
                self.assertEqual(jsonschema.check(RESPONSE_JSON_SCHEMA, data, Path('.')), [])
                proposals = _proposals(data)
                self.assertEqual(proposals[0]["body"]["ref"], "inp_Notice0001")
                self.assertEqual(jsonschema.errors("proposal", proposals[0]), [])

    def test_wire_citation_constraints_match_shared_boundary_cases(self):
        for ref in ("inp_Notice0001", "cap_Capability01", "evt_short", "", "evt_" + "a" * 41, "mem_bad-hyphen"):
            with self.subTest(ref=ref):
                data = self.wire([ref])
                with self.assertRaises(ValueError):
                    _proposals(data)
                self.assertTrue(jsonschema.check(RESPONSE_JSON_SCHEMA, data, Path('.')))


if __name__ == '__main__':
    unittest.main()
