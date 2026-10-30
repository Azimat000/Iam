"""Ссылки уведомления проходят реальный цикл и Codex-адаптер без раскрытия непрочитанного."""
import json

from cycle.helpers import CycleCase
from iam.providers.codex import CodexProvider, CodexSandboxConfig, fake
from iam.state import EventSpec, verify


class CodexCycleTest(CycleCase):
    def test_unread_notice_has_citable_event_and_codex_proposal_is_applied(self):
        cycle = self.build()
        calls = []

        def runner(argv, stdin, timeout, cleanup):
            calls.append(stdin)
            prompt = json.loads(stdin)['prompt'].split('\n\n', 1)[1]
            context = json.loads(prompt)['context']
            self.assertEqual(context[0]['ref'], trigger.event_id)
            return fake.reply([{'kind': 'no_action', 'rationale': 'notice only',
                                'cites': [context[0]['ref']], 'body': {'reason': 'rest'}}])

        cycle.provider = CodexProvider(runner, CodexSandboxConfig(), max_format_retries=0)
        trigger = self.c.record_event(EventSpec('input.arrived', 'rx:codex:notice', {
            'input_id': 'inp_UnreadNotice01', 'text': 'PRIVATE-UNREAD-CODEX', 'sender': 'operator'})).event
        report = cycle.run(trigger)
        self.assertEqual(report.outcome, 'done')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('PRIVATE-UNREAD-CODEX', calls[0])
        self.assertNotIn('input.read', self.types())
        self.assertIn('proposal.accepted', self.types())
        self.assertEqual(verify(self.c.store), [])
