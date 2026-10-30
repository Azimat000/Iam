import io
import json
import os
import traceback
import unittest
import zipfile
from unittest.mock import patch

from iam.backup import export_persona
from iam.telegram import Client, Receiver, Sender, TelegramError
from .helpers import TelegramCase, TOKEN, USER, update


class ClientTest(TelegramCase):
    def test_getme_and_method_whitelist(self):
        self.assertEqual(self.client.call("getMe", {})["id"], self.client.bot_id)
        self.api.mode = "no_length"
        self.assertEqual(self.client.call("getMe", {})["id"], self.client.bot_id)
        with self.assertRaises(ValueError):
            self.client.call("deleteWebhook", {})

    def test_errors_and_redirect_never_expose_token(self):
        for mode in ("403", "429", "500", "bad_json", "api_error", "bad_envelope", "bad_code", "drop", "redirect",
                     "truncated", "bad_status"):
            with self.subTest(mode=mode):
                self.api.mode = mode
                with self.assertRaises(TelegramError):
                    try:
                        self.client.call("getMe", {})
                    except TelegramError:
                        self.assertNotIn(TOKEN, traceback.format_exc())
                        raise
        self.assertEqual(len(self.api.calls), 11)  # редирект не исполнен

    def test_config_validation_and_bot_binding(self):
        for base in ("http://example.org", "https://example.org", self.api.url + "/path", self.api.url + "?x=1"):
            with self.assertRaises(ValueError):
                Client(base_url=base)
        for timeout in (-1, 51, True):
            with self.assertRaises(ValueError):
                Client(timeout=timeout)
        for users in ([], ["123"], [True], [-1], [0]):
            with self.assertRaises(ValueError):
                Receiver(self.c, self.client, users)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}), self.assertRaises(ValueError):
            Client()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "99999:" + "a" * 30}):
            other = Client(base_url=self.api.url)
        with self.assertRaises(ValueError):
            Sender(self.c, other, [USER])
        for limit in (-1, 11, True):
            with self.assertRaises(ValueError):
                Sender(self.c, self.client, [USER], max_resend=limit)

    def test_canary_not_in_database_logs_or_export(self):
        self.api.updates = [update(text=TOKEN)]  # секрет, присланный самим пользователем, экспорт тоже убирает
        logs = io.StringIO()
        with patch("sys.stderr", logs), patch("sys.stdout", logs):
            self.rx.poll_once()
            self.api.mode = "403"
            with self.assertRaises(TelegramError):
                self.rx.poll_once()
        self.assertNotIn(TOKEN, logs.getvalue())
        metadata = self.c.store.con.execute("SELECT value FROM meta").fetchall()
        self.assertNotIn(TOKEN, repr([tuple(r) for r in metadata]))
        out = self.tmp / "export.zip"
        export_persona(self.db, out)
        with zipfile.ZipFile(out) as archive:
            for name in archive.namelist():
                self.assertNotIn(TOKEN.encode(), archive.read(name))
