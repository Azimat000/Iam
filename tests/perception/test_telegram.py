import json

from iam.cycle import Cycle, Runner
from iam.cycle.runner import input_id_for
from iam.memory import PersistentMemory
from iam.stubs import Reply, ScriptedProvider, StubCapabilities, StubWorld, attend, rest
from iam.telegram import CreatorBridge

from telegram.helpers import TelegramCase, USER, update


class TelegramPerceptionTest(TelegramCase):
    def test_paused_receiver_resume_choice_read_followup_no_duplicate(self):
        memory = PersistentMemory(self.c)
        iid = input_id_for('telegram', '10')
        provider = ScriptedProvider([Reply((attend('dialogue', iid),)), Reply((rest(),))])
        bridge = CreatorBridge(self.rx, self.tx, USER)
        cycle = Cycle(self.c, provider, memory, StubWorld(), StubCapabilities(), bridge)
        runner = Runner(cycle, self.c, bridge, max_cycles=1)
        self.api.updates = [update(text='ТЕЛЕГРАМ_СКРЫТЫЙ_ТЕКСТ')]
        self.assertEqual(runner.run_pending(), [])
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.rx.notices()['count'], 1)
        self.assertEqual(cycle.perception.notices()['count'], 0)
        with self.assertRaises(PermissionError):
            self.rx.read_content(iid)
        self.c.start_session()
        self.assertEqual(runner.run_pending()[0].outcome, 'done')
        self.assertNotIn('ТЕЛЕГРАМ_СКРЫТЫЙ_ТЕКСТ', json.dumps(provider.calls[0], ensure_ascii=False))
        self.assertEqual(self.rx.read_content(iid), {'text': 'ТЕЛЕГРАМ_СКРЫТЫЙ_ТЕКСТ'})
        self.assertEqual(self.rx.notices()['count'], 0)
        self.assertEqual(runner.run_pending()[0].outcome, 'done')
        self.assertIn('ТЕЛЕГРАМ_СКРЫТЫЙ_ТЕКСТ', json.dumps(provider.calls[1], ensure_ascii=False))
        self.assertEqual(runner.run_pending(), [])
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(self.api.messages, [])
        self.assertEqual(len([e for e in self.c.events_after(0) if e.type == 'input.read']), 1)

    def test_many_notifications_can_be_ignored_without_answering(self):
        memory = PersistentMemory(self.c)
        provider = ScriptedProvider([Reply((rest('Останусь со своим делом'),)) for _ in range(4)])
        bridge = CreatorBridge(self.rx, self.tx, USER)
        cycle = Cycle(self.c, provider, memory, StubWorld(), StubCapabilities(), bridge)
        runner = Runner(cycle, self.c, bridge, max_cycles=4)
        self.api.updates = [update(n, text='СЕКРЕТ_' + str(n)) for n in range(10, 14)]
        self.c.start_session()
        reports = runner.run_pending()
        self.assertEqual([r.outcome for r in reports], ['done'] * 4)
        self.assertNotIn('input.read', self.types(self.c))
        self.assertEqual(len(provider.calls), 4)
        self.assertNotIn('СЕКРЕТ_', json.dumps(provider.calls, ensure_ascii=False))
        self.assertEqual(self.rx.notices()['count'], 4)
        self.assertEqual(self.api.messages, [])
        self.assertEqual(memory.records(), [])
