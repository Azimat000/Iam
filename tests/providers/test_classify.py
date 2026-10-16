"""Классификатор результата запуска (R-109): таблица «что вернул исполнитель → класс»."""
import json
import unittest

from iam.providers import Kind, RunResult, classify
from iam.providers import fake


def run(**env):
    return RunResult(0, json.dumps(env))


class ClassifierTable(unittest.TestCase):
    def test_table(self):
        cases = [
            ("успех, structured_output", fake.reply([{"kind": "no_action", "rationale": "r", "body": {"reason": "x"}}]), Kind.OK),
            ("успех, JSON в тексте", fake.reply_text('{"proposals": []}'), Kind.OK),
            ("успех, JSON в блоке ```json```", fake.reply_text('```json\n{"proposals": []}\n```'), Kind.OK),
            ("болтовня вместо JSON", fake.garbage(), Kind.FORMAT),
            ("JSON, но не объект", fake.reply_text("[1, 2]"), Kind.FORMAT),
            ("result не строка и нет structured_output", run(is_error=False, result=None), Kind.FORMAT),
            ("не JSON на stdout, код 0", RunResult(0, "просто текст"), Kind.FORMAT),
            ("пустой stdout, код 0", RunResult(0, ""), Kind.FORMAT),
            ("не JSON на stdout, код ≠ 0", RunResult(1, "", "boom"), Kind.EXEC),
            ("docker не запущен", fake.docker_failed(), Kind.EXEC),
            ("программа не найдена", RunResult(127, "", "не найдена программа: docker"), Kind.EXEC),
            ("реальный образец: 401", fake.auth_error(), Kind.AUTH),
            ("403", fake.error(403, "Forbidden"), Kind.AUTH),
            ("429", fake.rate_limited(), Kind.LIMIT),
            ("529 перегрузка", fake.overloaded(), Kind.UNAVAILABLE),
            ("500", fake.error(500, "Internal server error"), Kind.UNAVAILABLE),
            ("нет статуса, текст про лимит", fake.error(None, "Claude usage limit reached"), Kind.LIMIT),
            ("нет статуса, текст про вход", fake.error(None, "Please run /login · token expired"), Kind.AUTH),
            ("нет статуса, текст про сеть", fake.error(None, "connection reset by peer"), Kind.UNAVAILABLE),
            ("нет статуса, непонятно", fake.error(None, "что-то странное"), Kind.UNKNOWN),
            ("лимит аккаунта называет и аккаунт, и лимит", fake.error(None, "Your account usage limit was reached"), Kind.LIMIT),
            ("400 без пояснений", fake.error(400, "bad request"), Kind.UNKNOWN),
            ("таймаут", fake.hang(), Kind.TIMEOUT),
            ("попытка инструмента", fake.tool_attempt(), Kind.TOOL_USE),
        ]
        for title, result, expected in cases:
            with self.subTest(title):
                self.assertEqual(classify(result).kind, expected)

    def test_exit_code_is_not_trusted(self):
        """Реальная ошибка входа вышла с кодом 0, а упавший процесс с мусором — с кодом ≠ 0: смотрим на содержимое."""
        auth = fake.auth_error()
        self.assertEqual(auth.returncode, 0)
        self.assertEqual(classify(auth).kind, Kind.AUTH)
        failed = RunResult(1, json.dumps({"is_error": False, "result": '{"proposals": []}'}))
        self.assertEqual(classify(failed).kind, Kind.OK)

    def test_tool_use_keeps_parsed_data_for_the_journal(self):
        c = classify(fake.tool_attempt([{"kind": "no_action", "rationale": "r", "body": {"reason": "x"}}]))
        self.assertEqual((c.kind, len(c.data["proposals"])), (Kind.TOOL_USE, 1))
        self.assertEqual(len(c.envelope["permission_denials"]), 1)
        self.assertIsNone(classify(run(is_error=False, result="не json", permission_denials=[{"tool_name": "Bash"}])).data)

    def test_envelope_has_metadata_but_never_the_result_text(self):
        c = classify(fake.error(429, "секретный текст ответа", terminal="api_error"))
        self.assertEqual(c.envelope["api_error_status"], 429)
        self.assertNotIn("секретный", json.dumps(c.envelope, ensure_ascii=False))
        self.assertIn("секретный", c.message)           # текст ошибки — в сообщении, обрезанный
        long = classify(fake.error(500, "x" * 1000))
        self.assertEqual(len(long.message), 300)

    def test_real_auth_error_sample_is_the_one_captured_in_dev(self):
        sample = json.loads(fake.auth_error().stdout)
        self.assertEqual((sample["is_error"], sample["api_error_status"], sample["terminal_reason"]), (True, 401, "api_error"))


if __name__ == "__main__":
    unittest.main()
