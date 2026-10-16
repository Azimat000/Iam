"""Контрактные тесты ClaudeCodeProvider на FakeClaude: валидный ответ, мусор, попытка инструмента, зависание,
маршрутизация моделей, один повтор по формату, передача контекста только через потоки (R-109, R-116, R-192–R-194, R-208)."""
import json
import unittest

from iam.backup.scan import SecretScanner
from iam.domain import jsonschema
from iam.interfaces import CognitiveProvider
from iam.providers import (ClaudeCodeProvider, FakeClaude, ModelRouting, ProviderAuthError, ProviderFormatError,
                           ProviderLimitError, ProviderTimeout, ProviderUnavailable, SandboxConfig, build_prompt)
from iam.providers import fake

CANARY_PERSONA = "per_CanaryPersona99"
CANARY_SESSION = "ses_CanarySession99"
NOTE = {"kind": "no_action", "rationale": "ничего не нужно", "cites": [], "body": {"reason": "тихо"}}


def request(purpose="choose_action", **over):
    req = {"request_id": "req_Provider0001", "persona_id": CANARY_PERSONA, "session_id": CANARY_SESSION, "epoch": 3,
           "state_version": 17, "issued_at": "2026-10-16T10:00:00.000Z", "life_time_ms": 123456, "purpose": purpose,
           "context": [{"ref": "evt_AAAAAAAA", "origin": "observation", "layer": "state", "text": "Наступил плановый момент."},
                       {"ref": "mem_BBBBBBBB", "origin": "stated_by_other", "layer": "personal", "text": "Оператор любит чай."}],
           "allowed_proposals": ["action", "no_action"], "allowed_actions": ["cap_Notes00001"],
           "limits": {"max_output_tokens": 500, "deadline_ms": 60000, "attempt": 0},
           "response_schema": "response.schema.json", "idempotency_key": "req:req_Provider0001", "cycle_id": "cyc_Provider0001"}
    req.update(over)
    return req


def provider(*results, **kw):
    fk = FakeClaude(results)
    return ClaudeCodeProvider(fk, SandboxConfig(), **kw), fk


class ValidAnswerTest(unittest.TestCase):
    def test_valid_answer_becomes_a_schema_valid_response_with_our_ids(self):
        p, fk = provider(fake.reply([NOTE], "объяснение"))
        self.assertIsInstance(p, CognitiveProvider)
        resp = p.propose(request())
        self.assertEqual(jsonschema.errors("response", resp), [])
        self.assertEqual((resp["request_id"], resp["epoch"], resp["state_version"]), ("req_Provider0001", 3, 17))
        self.assertEqual((resp["provider"], resp["status"], resp["tool_use_detected"]),
                         ({"name": "claude-code", "model": "sonnet"}, "ok", False))
        (pr,) = resp["proposals"]
        self.assertRegex(pr["proposal_id"], r"^prop_[A-Za-z0-9]{8,40}$")
        self.assertEqual((pr["kind"], resp["explanation"], resp["usage"]["calls"]), ("no_action", "объяснение", 1))
        self.assertEqual(len(fk.calls), 1)

    def test_json_in_text_and_fenced_json_are_accepted(self):
        for result in (fake.reply_text(json.dumps({"proposals": [NOTE]})),
                       fake.reply_text("```json\n" + json.dumps({"proposals": [NOTE]}) + "\n```")):
            p, _ = provider(result)
            self.assertEqual(len(p.propose(request())["proposals"]), 1)

    def test_model_cannot_choose_proposal_ids_or_forge_the_envelope(self):
        forged = {**NOTE, "proposal_id": "prop_FORGED00001"}
        p, _ = provider(fake.reply([forged, forged]))
        resp = p.propose(request())
        ids = [x["proposal_id"] for x in resp["proposals"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertNotIn("prop_FORGED00001", ids)
        # поля конверта (эхо, provider, usage, tool_use_detected) модель задать не может — даже если попробует
        sneaky = fake.reply_text(json.dumps({"proposals": [NOTE], "tool_use_detected": False, "request_id": "req_Other00001",
                                             "state_version": 999, "provider": {"name": "x", "model": "y"}}))
        resp = provider(sneaky)[0].propose(request())
        self.assertEqual((resp["request_id"], resp["state_version"], resp["provider"]["name"]), ("req_Provider0001", 17, "claude-code"))

    def test_missing_explanation_is_empty_text(self):
        p, _ = provider(fake.reply_text(json.dumps({"proposals": []})))
        self.assertEqual(p.propose(request())["explanation"], "")
        p, _ = provider(fake.reply_text(json.dumps({"proposals": [], "explanation": 5})))
        self.assertEqual(p.propose(request())["explanation"], "")


class FakeClaudeTest(unittest.TestCase):
    def test_queue_and_exhaustion(self):
        fk = FakeClaude().queue(fake.hang())
        self.assertTrue(fk(["x"], "", 1.0).timed_out)
        with self.assertRaises(AssertionError):
            fk(["x"], "", 1.0)
        self.assertEqual(len(fk.calls), 2)


class FormatRetryTest(unittest.TestCase):
    """R-194: не более одной повторной попытки исправить неверный формат."""

    def test_one_repair_attempt_then_success(self):
        p, fk = provider(fake.garbage(), fake.reply([NOTE]))
        resp = p.propose(request())
        self.assertEqual((len(fk.calls), p.last_calls, resp["usage"]["calls"]), (2, 2, 2))
        self.assertIn("previous reply was rejected", fk.calls[1]["stdin"])
        self.assertNotIn("previous reply was rejected", fk.calls[0]["stdin"])

    def test_second_bad_attempt_ends_with_format_error(self):
        p, fk = provider(fake.garbage(), fake.garbage(), fake.reply([NOTE]))
        with self.assertRaises(ProviderFormatError):
            p.propose(request())
        self.assertEqual(len(fk.calls), 2)         # третий результат так и не понадобился

    def test_schema_violation_counts_as_bad_format(self):
        bad = fake.reply([{"kind": "action", "rationale": "r", "body": {"capability_id": "нет-такого", "params": {}}}])
        p, fk = provider(bad, fake.reply([NOTE]))
        self.assertEqual(p.propose(request())["proposals"][0]["kind"], "no_action")
        self.assertIn("proposals[0]", fk.calls[1]["stdin"])
        for broken in ({"proposals": "строка"}, {"proposals": ["не объект"]}, {"нет": "proposals"}):
            p, _ = provider(fake.reply_text(json.dumps(broken)), fake.reply_text(json.dumps(broken)))
            with self.assertRaises(ProviderFormatError):
                p.propose(request())

    def test_retries_can_be_switched_off(self):
        p, fk = provider(fake.garbage(), fake.reply([NOTE]), max_format_retries=0)
        with self.assertRaises(ProviderFormatError):
            p.propose(request())
        self.assertEqual(len(fk.calls), 1)


class ErrorClassesTest(unittest.TestCase):
    def test_each_failure_class_has_its_own_exception(self):
        table = [(fake.auth_error(), ProviderAuthError), (fake.rate_limited(), ProviderLimitError),
                 (fake.overloaded(), ProviderUnavailable), (fake.hang(), ProviderTimeout),
                 (fake.docker_failed(), ProviderUnavailable), (fake.error(None, "что-то странное"), ProviderUnavailable)]
        for result, exc in table:
            with self.subTest(exc=exc.__name__, text=result.stdout[:40]):
                p, fk = provider(result)
                with self.assertRaises(exc):
                    p.propose(request())
                self.assertEqual(len(fk.calls), 1)       # ошибки среды без повторов: решает пауза, а не адаптер

    def test_tool_attempt_makes_the_result_unusable_but_is_reported_not_raised(self):
        p, _ = provider(fake.tool_attempt([NOTE]))
        resp = p.propose(request())
        self.assertTrue(resp["tool_use_detected"])
        self.assertEqual(jsonschema.errors("response", resp), [])
        p, _ = provider(fake.tool_attempt())
        self.assertEqual(p.propose(request())["proposals"], [])


class StreamsAndMinimisationTest(unittest.TestCase):
    """R-116: контекст — в stdin, результат — из stdout. R-208/G09: в облако уходит только необходимое."""

    def test_context_goes_through_stdin_only(self):
        p, fk = provider(fake.reply([NOTE]))
        p.propose(request())
        call = fk.calls[0]
        for fragment in ("Наступил плановый момент.", "Оператор любит чай.", "cap_Notes00001"):
            self.assertIn(fragment, call["stdin"])
            self.assertNotIn(fragment, " ".join(call["argv"]))      # в командной строке контекста нет
        self.assertNotIn("-v", call["argv"])                         # и файлов обмена нет: ни одного монтирования
        self.assertEqual(call["cleanup"][:2], ["docker", "kill"])

    def test_nothing_unnecessary_reaches_the_model(self):
        p, fk = provider(fake.reply([NOTE]))
        p.propose(request())
        sent = fk.calls[0]["stdin"] + " ".join(fk.calls[0]["argv"])
        for secret in (CANARY_PERSONA, CANARY_SESSION, "req_Provider0001", "cyc_Provider0001", "2026-10-16T10", "123456",
                       "idempotency", "response.schema"):
            self.assertNotIn(secret, sent, secret)
        self.assertEqual(SecretScanner(canaries=[CANARY_PERSONA, CANARY_SESSION]).find(sent), [])
        self.assertEqual(sorted(json.loads(build_prompt(request()))), ["allowed_actions", "allowed_proposals", "context",
                                                                      "max_output_tokens", "purpose"])
        item = json.loads(build_prompt(request()))["context"][0]
        self.assertEqual(sorted(item), ["layer", "origin", "ref", "text"])

    def test_unread_content_is_not_something_the_provider_can_add(self):
        """Провайдер берёт только `context` запроса: лишние поля запроса (например, текст непрочитанного) не уходят."""
        req = request(unread_text="СЕКРЕТНЫЙ-ТЕКСТ-НЕПРОЧИТАННОГО")
        p, fk = provider(fake.reply([NOTE]))
        p.propose(req)
        self.assertNotIn("СЕКРЕТНЫЙ", fk.calls[0]["stdin"] + " ".join(fk.calls[0]["argv"]))


class RoutingTest(unittest.TestCase):
    def test_simple_tasks_go_to_the_cheap_model_and_only_listed_ones_to_the_strong(self):
        routing = ModelRouting(strong_purposes=frozenset({"reflect", "generalize"}))
        cheap = [pu for pu in ("appraise", "choose_action", "compose_message", "summarize") if routing.model_for(pu) == "sonnet"]
        strong = [pu for pu in ("reflect", "generalize") if routing.model_for(pu) == "opus"]
        self.assertEqual((cheap, strong), (["appraise", "choose_action", "compose_message", "summarize"], ["reflect", "generalize"]))
        for purpose in ("appraise", "choose_action", "compose_message", "reflect", "summarize", "generalize"):
            p, fk = provider(fake.reply([NOTE]), routing=routing)
            resp = p.propose(request(purpose))
            argv = fk.calls[0]["argv"]
            self.assertEqual(argv[argv.index("--model") + 1], routing.model_for(purpose))
            self.assertEqual(resp["provider"]["model"], routing.model_for(purpose))

    def test_the_list_of_hard_operations_is_configuration(self):
        custom = ModelRouting(cheap="haiku", strong="fable", strong_purposes=frozenset({"summarize"}))
        self.assertEqual([custom.model_for(x) for x in ("summarize", "reflect")], ["fable", "haiku"])
        self.assertEqual(ModelRouting(strong_purposes=frozenset()).model_for("reflect"), "sonnet")
        with self.assertRaises(ValueError):
            ModelRouting(strong_purposes=frozenset({"думать"}))

    def test_json_schema_flag_can_be_switched_off(self):
        p, fk = provider(fake.reply_text(json.dumps({"proposals": []})), use_json_schema=False)
        p.propose(request())
        self.assertNotIn("--json-schema", fk.calls[0]["argv"])


if __name__ == "__main__":
    unittest.main()
