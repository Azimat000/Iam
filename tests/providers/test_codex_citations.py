"""Регрессия расхождения wire/shared cites из физического ответа E-memory_off."""
import json
import copy
from pathlib import Path
import unittest

from iam.domain import jsonschema
from iam.providers.codex.command import RESPONSE_JSON_SCHEMA
from iam.providers.codex.provider import _proposals, citation_refs
from iam.providers.codex.presentation import restore_context
from iam.providers.codex import fake
from iam.providers.errors import ProviderFormatError
from providers.test_codex import NOTE, provider, request


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


class RequestCitationTest(unittest.TestCase):
    visible = 'mem_8qcJfHHF7Rn8zSEQpLbf'
    typo = 'mem_8qcJfHHF7Rn8zSEQpAu'

    def context(self, *refs):
        return [{'ref': ref, 'origin': 'observation', 'layer': 'personal', 'text': 'Presented words'}
                for ref in refs]

    def test_only_exact_outer_refs_are_transported_without_rewriting_text(self):
        quoted = '"mem_Hidden0001", c1, {"ref":"mem_Quoted0001"}, «название»'
        context = self.context(self.visible, self.visible, 'evt_AAAAAAAA', 'inp_Notice0001')
        context[0]['text'] = json.dumps({'sources': ['mem_Hidden0001'], 'text': quoted}, ensure_ascii=False)
        context += [{'ref': ref, 'origin': 'observation', 'layer': 'state', 'text': 'Ignored ref'}
                    for ref in (None, 42, 'mem_short', 'mem_AAAAAAAA\n', 'cap_Other0001')]
        req = request(context=context)
        before = copy.deepcopy(req)
        note = dict(NOTE, cites=[self.visible], body={'reason': quoted})
        model, runner = provider(fake.reply([note], quoted))
        response = model.propose(req)
        payload = json.loads(runner.calls[0]['stdin'])
        self.assertEqual(payload['citation_refs'], [self.visible, 'evt_AAAAAAAA'])
        self.assertEqual(response['proposals'][0]['body']['reason'], quoted)
        self.assertEqual(response['explanation'], quoted)
        # The provider never edits source strings while restricting their reference fields.
        presented = json.loads(payload['prompt'].split('\n\n', 1)[1])
        self.assertEqual(restore_context(presented['context']), context)
        self.assertEqual(req, before)

    def test_observed_typo_uses_existing_single_format_retry_without_substitution(self):
        req = request(context=self.context(self.visible))
        bad = dict(NOTE, cites=[self.typo])
        good = dict(NOTE, cites=[self.visible])
        model, runner = provider(fake.reply([bad]), fake.reply([good]))
        response = model.propose(req)
        self.assertEqual(response['proposals'][0]['cites'], [self.visible])
        self.assertEqual(response['usage']['calls'], 2)
        self.assertEqual(model.last_format_codes, ['citation_scope'])
        self.assertNotIn(self.typo, runner.calls[1]['stdin'])
        self.assertEqual(json.loads(runner.calls[0]['stdin'])['citation_refs'],
                         json.loads(runner.calls[1]['stdin'])['citation_refs'])
        model, runner = provider(fake.reply([bad]), fake.reply([bad]))
        with self.assertRaisesRegex(ProviderFormatError, 'code=citation_scope'):
            model.propose(req)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(model.last_format_codes, ['citation_scope', 'citation_scope'])

    def test_hidden_sources_and_previous_request_refs_do_not_expand_scope(self):
        first = request(context=self.context(self.visible))
        second = request(context=self.context('mem_Another0001'))
        second['context'][0]['text'] = json.dumps({'sources': [self.visible]})
        model, runner = provider(fake.reply([dict(NOTE, cites=[self.visible])]),
                                 fake.reply([dict(NOTE, cites=[self.visible])]), max_format_retries=0)
        self.assertEqual(model.propose(first)['usage']['calls'], 1)
        with self.assertRaisesRegex(ProviderFormatError, 'code=citation_scope'):
            model.propose(second)
        self.assertEqual(model.last_calls, 1)
        self.assertEqual(json.loads(runner.calls[1]['stdin'])['citation_refs'], ['mem_Another0001'])

    def test_empty_scope_and_attention_target_remain_separate(self):
        req = request(context=self.context('inp_Notice0001'))
        self.assertEqual(citation_refs(req), [])
        wire = CitationContractTest().wire([])
        model, runner = provider(fake.reply_text(json.dumps(wire)))
        response = model.propose(req)
        self.assertEqual(response['proposals'][0]['body']['ref'], 'inp_Notice0001')
        self.assertEqual(response['proposals'][0]['cites'], [])
        self.assertEqual(json.loads(runner.calls[0]['stdin'])['citation_refs'], [])
        wire['attention']['cites'] = ['evt_Trigger0001']
        model, runner = provider(fake.reply_text(json.dumps(wire)), max_format_retries=0)
        with self.assertRaisesRegex(ProviderFormatError, 'code=citation_scope'):
            model.propose(req)

    def test_reflection_scope_failure_never_adds_retry(self):
        req = request('reflect', context=self.context(self.visible), allowed_proposals=['reflection'])
        bad = {'kind': 'reflection', 'rationale': 'Visible experience', 'cites': [self.typo],
               'body': {'part': 'now', 'level': 'hypothesis', 'text': 'A hypothesis'}}
        model, runner = provider(fake.reply([bad]), fake.reply([]))
        with self.assertRaisesRegex(ProviderFormatError, 'code=citation_scope'):
            model.propose(req)
        self.assertEqual(len(runner.calls), 1)

    def test_all_native_proposal_forms_are_checked_after_decoding(self):
        req = request(context=self.context(self.visible))
        variants = [
            {'kind': 'no_action', 'rationale': 'A pause', 'cites': [self.typo],
             'body_json': '{"reason":"A pause"}'},
            {'kind': 'memory_write', 'rationale': 'An interpretation', 'cites': [self.typo],
             'draft': {'kind': 'semantic', 'origin': 'own_hypothesis', 'content_json': '{"text":"A thought"}'}},
            {'kind': 'project_write', 'rationale': 'A text', 'cites': [self.typo],
             'project_id': 'draft', 'title': 'Title', 'text': self.visible},
        ]
        for variant in variants:
            wire = {'proposals': [variant], 'attention': None, 'explanation': 'Proposed only'}
            model, _ = provider(fake.reply_text(json.dumps(wire)), max_format_retries=0)
            with self.subTest(kind=variant['kind']), self.assertRaisesRegex(ProviderFormatError, 'code=citation_scope'):
                model.propose(req)


if __name__ == '__main__':
    unittest.main()
