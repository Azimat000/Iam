"""The model addresses the creator without receiving the configured Telegram ID."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from iam.domain import new_id
from iam.panel.runtime import Runtime
from iam.providers.codex.fake import FakeCodex, reply
from iam.state import EventSpec, StateController
from iam.storage import Store


class PanelTelegramTest(unittest.TestCase):
    def test_creator_alias_reaches_only_the_configured_chat(self):
        recipient = 876543210987
        text = "Ответ из тестовой панели"
        provider = FakeCodex([reply([{"kind": "message", "rationale": "Ответ на приветствие",
                                    "body": {"capability_id": "cap_Telegram01", "to": "creator", "text": text}}])])
        client = Mock(bot_id=123, timeout=0)
        client.call.side_effect = lambda method, params: ([] if method == "getUpdates" else {
            "message_id": 1, "chat": {"id": recipient, "type": "private"}})
        api = client.call
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            with Store.create(db, new_id("exp")) as store:
                StateController(store).create_persona()
            with patch("iam.panel.runtime.subprocess_runner", provider), patch("iam.panel.runtime.Client", return_value=client):
                runtime = Runtime(db, telegram_user=recipient, interval=0.01)
                runtime.start()
                try:
                    runtime.submit("command", "start", "проверка", max_calls=1).result(5)
                    runtime.inject = lambda: runtime.ctl.record_event(
                        EventSpec("input.arrived", "rx:test:1", {"text": "Привет"}))
                    runtime.submit("inject").result(5)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        view = runtime.submit("view").result(5)
                        if any(event["type"] == "message.out_sent" for event in view["events"]):
                            break
                    else:
                        self.fail("Подтверждённая отправка не появилась в журнале")
                finally:
                    runtime.close()
        calls = [call.args[1] for call in api.call_args_list if call.args[0] == "sendMessage"]
        self.assertEqual(calls, [{"chat_id": recipient, "text": text}])
        prompt = json.loads(provider.calls[0]["stdin"])["prompt"]
        self.assertIn("to: creator", prompt)
        self.assertNotIn(str(recipient), prompt)
