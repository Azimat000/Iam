"""Managed Codex sign-in through the panel, with a fake external process."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from iam.domain import new_id
from iam.panel.__main__ import main
from iam.panel.runtime import Runtime
from iam.providers.codex.auth_session import AUTH_EVENT
from iam.providers.codex.fake import FakeCodex, reply
from iam.providers.run import RunResult
from iam.state import EventSpec, StateController
from iam.storage import Store


class PanelAuthTest(unittest.TestCase):
    def test_cli_enables_writeback_only_in_live_mode(self):
        with patch("iam.panel.__main__.Runtime") as runtime, patch("iam.panel.__main__.uvicorn.run"):
            main(["--db", "/tmp/unused.db", "--codex-auth-file", "/tmp/managed.json",
                  "--persist-codex-auth-updates"])
            self.assertIs(runtime.call_args.kwargs["persist_auth_updates"], True)
            main(["--db", "/tmp/unused.db", "--codex-auth-file", "/tmp/managed.json"])
            self.assertIs(runtime.call_args.kwargs["persist_auth_updates"], False)
            with self.assertRaises(SystemExit):
                main(["--db", "/tmp/unused.db", "--demo", "--persist-codex-auth-updates"])

    def test_updated_credentials_are_reused_and_never_enter_the_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            db, auth = directory / "test.db", directory / "managed.json"
            credentials = {"auth_mode": "chatgpt", "tokens": {
                "access_token": "test-access", "id_token": "test-id", "refresh_token": "test-refresh"},
                "last_refresh": "2026-11-09T00:00:00+00:00"}
            auth.write_text(json.dumps(credentials))
            auth.chmod(0o600)
            updated = {**credentials, "tokens": {**credentials["tokens"], "access_token": "updated-test-access"}}
            response = reply([])
            handoff = json.dumps({"type": AUTH_EVENT, "auth": updated}) + "\n"
            fake = FakeCodex([RunResult(0, response.stdout + handoff)] * 2)
            with Store.create(db, new_id("exp")) as store:
                StateController(store).create_persona()
            with patch("iam.panel.runtime.subprocess_runner", fake):
                runtime = Runtime(db, auth_file=str(auth), persist_auth_updates=True, interval=0.01)
                runtime.start()
                try:
                    runtime.submit("command", "start", "проверка", max_calls=2).result(5)
                    runtime.inject = lambda number: runtime.ctl.record_event(
                        EventSpec("input.arrived", f"rx:test:{number}", {"text": "пример"}))
                    for number in (1, 2):
                        runtime.submit("inject", number).result(5)
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            view = runtime.submit("view").result(5)
                            if sum(e["type"] == "proposal.received" for e in view["events"]) >= number:
                                break
                        else:
                            self.fail("Ответ модели не зафиксирован")
                finally:
                    runtime.close()
            self.assertEqual(json.loads(fake.calls[0]["stdin"])["auth"], credentials)
            self.assertEqual(json.loads(fake.calls[1]["stdin"])["auth"], updated)
            self.assertEqual(json.loads(auth.read_text()), updated)
            with Store.open(db, readonly=True) as store:
                for event in store.events():
                    self.assertNotIn("updated-test-access", str(event.payload))
                    self.assertNotIn(AUTH_EVENT, str(event.payload))
