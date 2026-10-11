"""Сканер секретов (R-204, R-219): шаблоны, «канарейки», окружение, редактирование, чистота репозитория."""
import base64
import json
import subprocess
import unittest
from pathlib import Path

from iam.backup import REDACTED, SecretScanner, env_secrets

ROOT = Path(__file__).resolve().parents[2]
# Фальшивые, но похожие на настоящие секреты собираются из частей: в исходниках теста сканер их не увидит.
TG = "123456789" + ":" + "AAH" + "x" * 32
SK = "sk-" + "ant-" + "abcdefghij" * 3
GH = "ghp_" + "A1b2C3d4E5" * 4
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
JWT = "eyJ" + "hbGciOiJI" + "." + "eyJzdWIiOiIx" + "." + "SflKxwRJSMeKKF2QT4"


class PatternTest(unittest.TestCase):
    def kinds(self, text, **kw):
        return [f.kind for f in SecretScanner(use_env=False, **kw).find(text)]

    def test_each_pattern_is_found(self):
        cases = {
            "telegram_bot_token": f"токен бота {TG} вот",
            "provider_api_key": f"ключ {SK}",
            "github_token": f"gh {GH}",
            "aws_access_key": f"aws {AWS}",
            "slack_token": "slack " + "xoxb" + "-1234567890-abcdef",
            "jwt": f"jwt {JWT}",
            "private_key": "-----BEGIN " + "RSA PRIVATE KEY-----",
            "bearer_token": "Authorization: Bearer " + "abcdefghijklmnopqrstuvwxyz0123",
            "secret_assignment": "password = " + "hunter2hunter22",
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                self.assertIn(kind, self.kinds(text))

    def test_ordinary_text_is_not_flagged(self):
        for text in ["Она прочитала главу 1 и вспомнила чай с мятой.", "2026-10-11T20:53:00.123Z", "evt_AbCdEfGhIj12",
                     "rx:tg:123456789:1", "token: короткий", "пароль обсуждали вчера", "password: hunter"]:
            with self.subTest(text=text):
                self.assertEqual(self.kinds(text), [])

    def test_assignments_can_be_disabled_for_repository_scan(self):
        self.assertEqual(self.kinds("password = hunter2hunter22", assignments=False), [])

    def test_canary_forms_raw_json_escaped_and_base64(self):
        canary = 'ЗАПАСНОЙ"ключ\\42'
        sc = SecretScanner([canary], use_env=False)
        for form in (canary, json.dumps(canary)[1:-1], json.dumps(canary, ensure_ascii=False)[1:-1],
                     base64.b64encode(canary.encode()).decode().rstrip("="),
                     base64.urlsafe_b64encode(canary.encode()).decode().rstrip("=")):
            with self.subTest(form=form):
                self.assertEqual([f.kind for f in sc.find(f"до {form} после")], ["known_secret"])

    def test_short_canary_is_refused(self):
        with self.assertRaises(ValueError):
            SecretScanner(["abc"], use_env=False)

    def test_overlapping_findings_merge(self):
        sc = SecretScanner([TG[:20]], use_env=False)
        found = sc.find(TG)
        self.assertEqual([(f.start, f.end) for f in found], [(0, len(TG))])

    def test_partially_overlapping_findings_extend_the_first(self):
        sc = SecretScanner(["ABCDEFGHIJ", "FGHIJKLMNO"], use_env=False)
        self.assertEqual([(f.start, f.end) for f in sc.find("..ABCDEFGHIJKLMNO..")], [(2, 17)])

    def test_mask_never_shows_the_secret(self):
        sc = SecretScanner(use_env=False)
        text = f"x {TG} y"
        (f,) = sc.find(text)
        self.assertNotIn(TG, f.mask(text))
        self.assertEqual(f.mask(text), "12…(" + str(len(TG)) + " зн.)")
        (short,) = SecretScanner(["abcd"], use_env=False).find("abcd")
        self.assertEqual(short.mask("abcd"), "…")
        for line in sc.describe(text, "файл"):
            self.assertNotIn(TG, line)


class EnvAndRedactTest(unittest.TestCase):
    def test_env_secrets_by_name_and_length(self):
        env = {"IAM_TELEGRAM_TOKEN": "tok-" + "12345678", "HOME": "/home/user/longvalue", "MY_PASSWORD": "short",
               "API_KEY": "key-value-1234", "PATH": "/usr/bin"}
        self.assertEqual(env_secrets(env), ["key-value-1234", "tok-12345678"])
        sc = SecretScanner(use_env=True, environ=env)
        self.assertEqual(sc.known, ("key-value-1234", "tok-12345678"))
        self.assertEqual(len(sc.find("в тексте tok-12345678 и ещё")), 1)

    def test_env_is_read_from_os_environ_by_default(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"IAM_TEST_SECRET_X": "zz-secret-value-9"}):
            self.assertIn("zz-secret-value-9", env_secrets())

    def test_redact_text_and_structures(self):
        sc = SecretScanner(["canary-ЗЕБРА-77"], use_env=False)
        text, found = sc.redact("a canary-ЗЕБРА-77 b " + TG)
        self.assertEqual(text, f"a {REDACTED} b {REDACTED}")
        self.assertEqual(len(found), 2)
        counts = {}
        out = sc.redact_value({"canary-ЗЕБРА-77": ["x canary-ЗЕБРА-77", 5, None, {"k": TG}], "n": 1}, counts)
        self.assertEqual(out, {REDACTED: [f"x {REDACTED}", 5, None, {"k": REDACTED}], "n": 1})
        self.assertEqual(counts, {"known_secret": 2, "telegram_bot_token": 1})
        self.assertEqual(sc.redact_value("чисто"), "чисто")
        self.assertEqual(sc.redact_value(["canary-ЗЕБРА-77"]), [REDACTED], "без счётчика тоже работает")


class RepositoryIsCleanTest(unittest.TestCase):
    def test_no_secrets_in_tracked_files(self):
        """R-219: сканер проходит по файлам репозитория (шаблоны высокой точности) — секретов нет."""
        try:
            files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("не git-репозиторий")
        sc = SecretScanner(use_env=False, assignments=False)
        leaks = []
        for rel in files:
            p = ROOT / rel
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            leaks += sc.describe(text, rel)
        self.assertEqual(leaks, [])
