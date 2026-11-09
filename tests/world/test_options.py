"""Варианты остаются гипотезами до отдельного полного решения личности."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

from iam.backup.scan import SecretScanner
from iam.domain import new_id
from iam.interfaces import ProviderError
from iam.state import verify
from iam.stubs import Reply, rest
from iam.world.cycle import LivingCycle
from iam.world.options import OptionsCycle
from world.helpers import LivingCase


class OptionsTest(LivingCase):
    def rebuild(self, cycle_type):
        from world.test_prospective import ProspectiveTest
        ProspectiveTest.rebuild(self, cycle_type)

    def setUp(self):
        super().setUp()
        self.source = self.read()
        self.rebuild(OptionsCycle)
        self.req = self.cycle._request(new_id('cyc'), [
            {'ref': self.source.memory_id, 'origin': 'observation', 'layer': 'personal', 'text': self.source.text}
        ], 0, None, purpose='appraise')
        self.req['allowed_proposals'] = ['memory_write', 'intention']

    def prepare(self, reply):
        self.p.queue(reply)
        return self.cycle._prepare_appraisal(copy.deepcopy(self.req), [self.source])

    def audit(self):
        return [event.payload for event in self.c.store.events()
                if event.payload.get('phase') == 'future_option_search'][-1]

    def test_candidate_is_not_goal_memory_or_later_context_until_full_commit(self):
        candidate = self.goal(self.source, iid='possibility')
        before_memories = [row.to_dict() for row in self.m.records()]
        def decline(request):
            self.assertFalse(self.c.state['intentions'])
            self.assertEqual([row.to_dict() for row in self.m.records()], before_memories)
            packet = request['context'][-1]
            self.assertEqual(packet['origin'], 'imagination')
            self.assertEqual(len(packet['text']), self.cycle.option_buffer_chars)
            value = json.loads(packet['text'])
            self.assertFalse(value['is_commitment'])
            self.assertEqual(value['options'][0]['body'], candidate['body'])
            self.assertNotIn('wants', packet['text'])
            return Reply(())
        report = self.run_reply(candidate, replies=(decline, Reply((rest(),))))
        self.assertEqual(report.outcome, 'done')
        self.assertFalse(self.c.state['intentions'])
        self.assertNotIn('possibility', json.dumps(self.p.calls[-1]['context']))
        self.assertEqual([row.to_dict() for row in self.m.records()], before_memories)
        self.assertEqual(verify(self.c.store), [])
        self.assertEqual(len(self.audit()['candidates']), 1)
        self.assertFalse(self.audit()['selected'])
        self.assertTrue(self.audit()['hypothetical'])
        self.rebuild(OptionsCycle)
        self.assertEqual(self.run_reply(rest()).outcome, 'done')
        self.assertEqual(self.p.calls[-1]['purpose'], 'choose_action')
        self.assertNotIn('possibility', json.dumps(self.p.calls[-1]['context']))

    def test_selector_can_accept_candidate_or_make_independent_alternative(self):
        candidate = self.goal(self.source, iid='hypothesis')
        selected = self.goal(self.source, iid='chosen')
        report = self.run_reply(candidate, replies=(Reply((selected,)), Reply((rest(),))))
        self.assertEqual(report.outcome, 'done')
        self.assertEqual(set(self.c.state['intentions']), {'chosen'})
        self.assertEqual(self.c.state['intentions']['chosen']['sources'], [self.source.memory_id])
        self.assertIn('chosen', json.dumps(self.p.calls[-1]['context']))
        self.assertNotIn('intention', self.p.calls[-1]['allowed_proposals'])
        self.assertEqual([q['purpose'] for q in self.p.calls[-3:]], ['appraise','appraise','choose_action'])
        self.assertNotEqual(self.p.calls[-3]['request_id'], self.p.calls[-2]['request_id'])
        self.assertEqual(self.p.calls[-3]['allowed_proposals'], ['intention'])
        self.assertEqual(verify(self.c.store), [])

    def test_selector_may_explicitly_commit_the_original_candidate(self):
        candidate = self.goal(self.source, iid='selected_option')
        selected = copy.deepcopy(candidate)
        selected['proposal_id'] = new_id('prop')
        report = self.run_reply(candidate, replies=(Reply((selected,)), Reply((rest(),))))
        self.assertEqual(report.outcome, 'done')
        self.assertEqual(set(self.c.state['intentions']), {'selected_option'})
        self.assertEqual(self.c.state['intentions']['selected_option']['criterion'], candidate['body']['criterion'])
        self.assertEqual(verify(self.c.store), [])

    def test_hidden_options_do_not_leak_candidates_or_explanation(self):
        self.cycle.expose_options = False
        request = self.prepare(Reply((self.goal(self.source, iid='hidden_option'),), explanation='HIDDEN_REASON'))
        value = json.loads(request['context'][-1]['text'])
        self.assertEqual(value['options'], [])
        self.assertNotIn('hidden_option', json.dumps(request))
        self.assertNotIn('HIDDEN_REASON', json.dumps(request))
        self.assertEqual(len(request['context'][-1]['text']), self.cycle.option_buffer_chars)
        self.assertEqual(len(self.audit()['candidates']), 1)
        self.assertFalse(self.c.state['intentions'])

    def test_empty_generation_is_allowed_and_does_not_remove_commit_permission(self):
        request = self.prepare(Reply(()))
        self.assertEqual(json.loads(request['context'][-1]['text'])['options'], [])
        self.assertEqual(request['allowed_proposals'], ['memory_write', 'intention'])
        self.assertFalse(self.c.state['intentions'])

    def test_ineligible_candidates_are_discarded_without_changing_state(self):
        for mode in ('wrong_kind','transition','no_new_basis','hidden_basis','oversize','duplicate','no_capability'):
            with self.subTest(mode=mode):
                goal = self.goal(self.source)
                proposals = [goal]
                if mode == 'wrong_kind': proposals = [rest()]
                elif mode == 'transition': goal['body']['op'] = 'transition'
                elif mode == 'no_new_basis': goal['cites'] = []
                elif mode == 'hidden_basis': goal['cites'].append(new_id('mem'))
                elif mode == 'oversize': goal['rationale'] = 'x' * 1490
                elif mode == 'duplicate': proposals.append(copy.deepcopy(goal)); proposals[-1]['proposal_id'] = new_id('prop')
                elif mode == 'no_capability': goal['body']['operation'] = 'unknown'
                self.prepare(Reply(tuple(proposals)))
                audit = self.audit()
                self.assertEqual(len(audit['rejected']), 1)
                self.assertEqual(len(audit['candidates']), int(mode == 'duplicate'))
                self.assertFalse(self.c.state['intentions'])

    def test_candidate_cannot_return_forgotten_text_or_reference_in_its_description(self):
        self.rebuild(LivingCycle)
        hidden = self.write("Это содержание закрытой записи.", project="private")
        self.assertTrue(self.m.fade(hidden.memory_id, availability=0, detail=0).applied)
        self.rebuild(OptionsCycle)
        self.req["state_version"] = self.c.state_version
        text_leak = self.goal(self.source, iid="text_leak", text=hidden.text)
        ref_leak = self.goal(self.source, iid="ref_leak")
        ref_leak["rationale"] = hidden.memory_id
        valid = self.goal(self.source, iid="allowed")
        request = self.prepare(Reply((text_leak, ref_leak, valid)))
        options = json.loads(request["context"][-1]["text"])["options"]
        self.assertEqual([o["body"]["intention_id"] for o in options], ["allowed"])
        self.assertEqual(set(self.audit()["rejected"]), {text_leak["proposal_id"], ref_leak["proposal_id"]})

    def test_invalid_echo_tools_secret_or_count_never_reaches_commit(self):
        for mode in ('malformed','stale','tools','secret','many'):
            with self.subTest(mode=mode):
                proposals = (self.goal(self.source),)
                if mode == 'many': proposals = tuple(self.goal(self.source, iid='option_'+str(i)) for i in range(4))
                reply = Reply(proposals)
                if mode == 'malformed': reply.raw = {'invalid': True}
                elif mode == 'stale': reply.extra = {'epoch': self.c.epoch-1}
                elif mode == 'tools': reply.tool_use = True
                elif mode == 'secret': reply.explanation = 'PRIVATE_SECRET'
                self.cycle.provider.scanner = SecretScanner(canaries=['PRIVATE_SECRET'], use_env=False)
                with self.assertRaises(ProviderError): self.prepare(reply)
                self.assertTrue(self.audit()['error'])
                self.assertFalse(self.c.state['intentions'])

    def test_session_change_or_provider_error_is_audited_without_commit(self):
        with self.assertRaises(RuntimeError): self.prepare(Reply(error=RuntimeError('unavailable')))
        self.assertEqual(self.audit()['error'], 'RuntimeError')
        def pause(request): self.c.pause_session('operator')
        with self.assertRaisesRegex(ProviderError,'options_state_changed'):
            self.prepare(Reply((self.goal(self.source),), before=pause))
        self.assertEqual(self.audit()['error'], 'options_state_changed')
        self.assertFalse(self.c.state['intentions'])

    def test_state_change_during_audit_stops_before_commit_call(self):
        original = self.cycle.provider.record
        def record(rid, value):
            saved = original(rid, value)
            self.assertTrue(self.c.pause_session('operator').applied)
            self.assertTrue(self.c.resume_session().applied)
            return saved
        before = len(self.p.calls)
        with patch.object(self.cycle.provider, 'record', side_effect=record):
            report = self.run_reply(self.goal(self.source))
        self.assertEqual(report.reason, 'appraisal_preparation_error')
        self.assertEqual(len(self.p.calls), before + 1)
        self.assertFalse(self.c.state['intentions'])
        self.assertFalse(any(event.type == 'proposal.requested' and event.payload.get('request', {}).get('purpose') == 'appraise'
                             for event in self.c.store.events()))

    def test_audit_refusal_and_packet_overflow_stop_without_commit(self):
        with patch.object(self.cycle.provider,'record',return_value=SimpleNamespace(event=None)):
            with self.assertRaisesRegex(ProviderError,'options_audit_unavailable'): self.prepare(Reply(()))
        self.cycle.option_buffer_chars = 1
        with self.assertRaisesRegex(ProviderError,'option_buffer_overflow'): self.prepare(Reply(()))
        self.assertFalse(self.c.state['intentions'])

    def test_generation_and_audit_share_original_deadline_without_extension(self):
        for moments in ((0,61),(0,0,61),(0,1,2)):
            with self.subTest(moments=moments), patch('iam.world.options.time.monotonic',side_effect=moments):
                if moments[-1] == 61:
                    with self.assertRaisesRegex(ProviderError,'options_deadline'): self.prepare(Reply(()))
                else:
                    result = self.prepare(Reply(()))
                    self.assertEqual(result['limits']['deadline_ms'], 58000)
        self.assertFalse(self.c.state['intentions'])

    def test_bare_candidate_reference_or_candidate_event_is_not_a_live_goal_source(self):
        def select(request):
            proposal = self.goal(self.source)
            proposal['cites'] = [request['context'][-1]['ref']]
            return Reply((proposal,))
        report = self.run_reply(self.goal(self.source), replies=(select,))
        self.assertEqual(report.outcome,'no_decision')
        self.assertFalse(self.c.state['intentions'])
