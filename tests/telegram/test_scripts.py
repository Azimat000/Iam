import contextlib
import importlib.util
import io
import os
from pathlib import Path
from unittest.mock import patch

from .helpers import TelegramCase, USER, update

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("telegram_verify", ROOT / "scripts/telegram/verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class ScriptTest(TelegramCase):
    def run_script(self, *extra):
        output = io.StringIO()
        with patch.dict(os.environ, {"TELEGRAM_USER_ID": str(USER)}), patch.object(verify, "Client", return_value=self.client), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = verify.main(["--data-dir", str(self.tmp / "live-check"), *extra])
        return code, output.getvalue()

    def test_live_check_logic_on_fake_server_receiver_and_send(self):
        self.api.updates = [update()]
        code, output = self.run_script()
        self.assertEqual(code, 0, output)
        self.assertIn("Новых сохранено: 1", output)
        self.assertEqual(self.api.messages, [])
        code, output = self.run_script("--send-test")
        self.assertEqual(code, 0, output)
        self.assertIn("confirmed", output)
        self.assertEqual(len(self.api.messages), 1)
        self.assertEqual(self.api.messages[0]["chat_id"], USER)

    def test_script_failed_send_not_claimed_success(self):
        original = self.client.call
        def fail_send(method, params):
            if method == "sendMessage":
                self.api.mode = "drop"
            return original(method, params)
        with patch.object(self.client, "call", side_effect=fail_send):
            code, output = self.run_script("--send-test")
        self.assertEqual(code, 1)
        self.assertIn("unknown_outcome", output)

    def test_script_invalid_configuration_and_network_failure(self):
        self.api.mode = "403"
        code, output = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("не пройдена", output)
        for args in (["--data-dir", str(ROOT / "data")], ["--data-dir", str(self.tmp), "--polls", "0"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                verify.main(args)
        with patch.dict(os.environ, {"TELEGRAM_USER_ID": "username"}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verify.main(["--data-dir", str(self.tmp)]), 1)
