"""The real panel path supports repeated read/reply turns without optional model preludes."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from iam.domain import new_id
from iam.panel.runtime import Runtime
from iam.providers.codex.fake import FakeCodex, reply
from iam.state import EventSpec, StateController, verify
from iam.storage import Store


class ConversationRuntimeTest(unittest.TestCase):
    def test_six_read_reply_turns_keep_one_session_and_charge_twenty_three_calls(self):
        recipient = 876543210987
        provider = FakeCodex()
        client = Mock(bot_id=123, timeout=0)
        client.call.side_effect = lambda method, params: ([] if method == 'getUpdates' else {
            'message_id': 1, 'chat': {'id': recipient, 'type': 'private'}})
        api = client.call
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'test.db'
            with Store.create(db, new_id('exp')) as store:
                StateController(store).create_persona()
            with patch('iam.panel.runtime.subprocess_runner', provider), patch('iam.panel.runtime.Client', return_value=client):
                rt = Runtime(db, telegram_user=recipient, interval=0.01)
                rt.start()
                try:
                    rt.submit('command', 'conversation', 'обычный разговор').result(5)
                    sid = rt.submit('view').result(5)['usage']['session_id']
                    rt.can_review = lambda: rt.cycle.can_appraise()
                    self.assertTrue(rt.submit('can_review').result(5))
                    for number in range(6):
                        iid = new_id('inp')
                        sequence = [reply([{'kind': 'attend', 'rationale': 'Прочесть сообщение',
                            'body': {'focus': 'dialogue', 'ref': iid}}]),
                            reply([{'kind': 'message', 'rationale': 'Выбрать ответ',
                            'body': {'capability_id': 'cap_Telegram01', 'to': 'creator', 'text': f'Черновик {number}'}}]),
                            reply([{'kind': 'message', 'rationale': 'Ответить на прочитанное',
                            'body': {'capability_id': 'cap_Telegram01', 'to': 'creator', 'text': f'Ответ {number}'}}])]
                        if number:
                            sequence.insert(2, reply([{'kind': 'no_action', 'rationale': 'Проверка доступных источников',
                                'body': {'findings': [], 'uncertainty': 'В этом обмене нет спорного воспоминания.', 'reason': 'Нет спорного воспоминания'}}]))
                        provider.queue(*sequence)
                        rt.inject = lambda iid=iid, number=number: rt.ctl.record_event(EventSpec(
                            'input.arrived', 'rx:test:' + iid,
                            {'input_id': iid, 'sender': str(recipient), 'channel': 'telegram', 'text': f'Вопрос {number}'}))
                        rt.submit('inject').result(5)
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            view = rt.submit('view').result(5)
                            sent = [call for call in api.call_args_list if call.args[0] == 'sendMessage']
                            if len(sent) == number + 1:
                                break
                        else:
                            self.fail('Нет подтверждённого ответа на очередное сообщение')
                    self.assertEqual(view['usage']['calls'], 23)
                    self.assertEqual([call.args[1]['text'] for call in sent], [f'Ответ {n}' for n in range(6)])
                    self.assertEqual(view['usage']['session_id'], sid)
                    self.assertEqual(view['session_state'], 'running')
                    self.assertIsNone(view['usage']['max_calls'])
                    self.assertIsNone(view['usage']['max_ms'])
                    rt.submit('command', 'pause', 'ручная пауза').result(5)
                    self.assertEqual(rt.submit('view').result(5)['session_state'], 'paused')
                finally:
                    rt.close()
            with Store.open(db) as store:
                self.assertEqual(verify(store), [])
                requests = [e.payload['request'] for e in store.events() if e.type == 'proposal.requested']
                self.assertEqual([q['purpose'] for q in requests], ['choose_action'] * 12)
                composed = [e for e in store.events() if e.type == 'proposal.assessed'
                            and e.payload.get('phase') == 'conversation_composition']
                readings = [e for e in store.events() if e.type == 'proposal.assessed'
                            and e.payload.get('phase') == 'conversation_reading']
                self.assertEqual(len(readings), 5)
                self.assertTrue(all(e.payload['error'] is None for e in readings))
                self.assertEqual(len(composed),6)
                self.assertTrue(all(e.payload['error'] is None for e in composed))
        self.assertEqual(len(provider.calls), 23)
        prompts = [json.loads(call['stdin'])['prompt'] for call in provider.calls]
        self.assertNotIn('Вопрос 0', prompts[0])
        self.assertIn('Вопрос 0', prompts[1])
        self.assertIn('Вопрос 0', prompts[2])
