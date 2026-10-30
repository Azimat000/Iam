"""Contract and adversarial checks for isolated subscription Codex, using no live services."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iam.domain import jsonschema
from iam.interfaces import CognitiveProvider
from iam.providers.classify import Kind
from iam.providers.codex import (CodexProvider, CodexSandboxConfig, DEFAULT_MODEL, FakeCodex,
                                 RESPONSE_JSON_SCHEMA, build_prompt, classify, docker_run, fake)
from iam.providers.codex.auth import read_auth, validate_auth_path
from iam.providers.codex.command import REPO_ROOT, _size
from iam.providers.errors import (ProviderAuthError, ProviderFormatError, ProviderLimitError,
                                  ProviderTimeout, ProviderUnavailable, SessionLimitReached)
from iam.providers.run import RunResult, subprocess_runner

NOTE = {"kind": "no_action", "rationale": "nothing to do", "cites": [], "body": {"reason": "quiet"}}
AUTH = {"auth_mode": "chatgpt", "tokens": {"access_token": "secret-access", "id_token": "secret-id",
                                         "refresh_token": "secret-refresh", "account_id": "account"}}
WIRE = {"proposals": [{"kind": "no_action", "rationale": "why", "cites": [],
                       "body_json": '{"reason":"quiet"}'}], "explanation": "explanation"}


def request(purpose="choose_action", **over):
    result = {"request_id": "req_Provider0001", "persona_id": "per_CanaryPersona99", "session_id": "ses_CanarySession99",
              "epoch": 3, "state_version": 17, "purpose": purpose, "issued_at": "2026-10-16T10:00:00.000Z",
              "life_time_ms": 123456, "context": [{"ref": "evt_AAAAAAAA", "origin": "observation", "layer": "state",
                                                 "text": "minimal context", "extra": "private detail"}],
              "allowed_proposals": ["no_action"], "allowed_actions": [],
              "limits": {"max_output_tokens": 500, "deadline_ms": 60000, "attempt": 0},
              "idempotency_key": "private-idempotency", "unread": "private-unread"}
    result.update(over)
    return result


def provider(*results, **options):
    runner = FakeCodex(results)
    return CodexProvider(runner, CodexSandboxConfig(), **options), runner


def sequence(*middle, finish=True):
    events = [{"type": "thread.started", "thread_id": "thread"}, {"type": "turn.started"}, *middle]
    if finish:
        events.append({"type": "turn.completed"})
    return fake.events(*events)


def item(kind="agent_message", **fields):
    return {"type": "item.completed", "item": {"id": "item_0", "type": kind,
                                               "text": json.dumps(WIRE), **fields}}


class ProviderTests(unittest.TestCase):
    def test_shared_contract_luna_for_every_purpose_and_local_envelope(self):
        for purpose in ("appraise", "choose_action", "compose_message", "reflect", "summarize", "generalize"):
            p, runner = provider(fake.reply([NOTE], "explanation"))
            response = p.propose(request(purpose))
            self.assertIsInstance(p, CognitiveProvider)
            self.assertEqual(jsonschema.errors("response", response), [])
            self.assertEqual(response["provider"], {"name": "codex", "model": "gpt-6-luna"})
            self.assertEqual((response["request_id"], response["epoch"], response["state_version"]), ("req_Provider0001", 3, 17))
            self.assertEqual(response["explanation"], "explanation")
            self.assertEqual(response["usage"]["calls"], 1)
            self.assertFalse(response["tool_use_detected"])
            self.assertRegex(response["proposals"][0]["proposal_id"], r"^prop_[A-Za-z0-9]{8,40}$")
            self.assertEqual(runner.calls[0]["argv"][runner.calls[0]["argv"].index("--model") + 1], DEFAULT_MODEL)
        p, _ = provider(fake.reply([NOTE, NOTE]))
        proposals = p.propose(request())["proposals"]
        self.assertNotEqual(proposals[0]["proposal_id"], proposals[1]["proposal_id"])

    def test_context_minimization_and_stdin_only(self):
        p, runner = provider(fake.reply([{k: v for k, v in NOTE.items() if k != "cites"}]))
        p.propose(request())
        call = runner.calls[0]
        payload = json.loads(call["stdin"])
        self.assertEqual(set(payload), {"auth", "prompt"})
        self.assertEqual(payload["auth"], {})
        self.assertIn("minimal context", payload["prompt"])
        self.assertIn("body_json", payload["prompt"])
        self.assertNotIn("minimal context", str(call["argv"]))
        for private in ("per_Canary", "ses_Canary", "req_Provider", "2026-10-16", "123456", "private-"):
            self.assertNotIn(private, call["stdin"] + str(call["argv"]))
        self.assertEqual(set(json.loads(build_prompt(request()))),
                         {"purpose", "context", "allowed_proposals", "allowed_actions", "max_output_tokens"})
        self.assertEqual(set(json.loads(build_prompt(request()))["context"][0]), {"ref", "origin", "layer", "text"})
        self.assertEqual(call["cleanup"][:2], ["docker", "kill"])

    def test_retry_is_bounded_and_repair_does_not_echo_model_text(self):
        p, runner = provider(fake.garbage("secret model garbage"), fake.reply([NOTE]))
        self.assertEqual(p.propose(request())["usage"]["calls"], 2)
        self.assertIn("previous reply was rejected", runner.calls[1]["stdin"])
        self.assertNotIn("secret model garbage", runner.calls[1]["stdin"])
        for retries in (0, 1):
            p, runner = provider(fake.garbage(), fake.garbage(), fake.reply([]), max_format_retries=retries)
            with self.assertRaises(ProviderFormatError):
                p.propose(request())
            self.assertEqual(len(runner.calls), retries + 1)

    def test_wire_shape_and_common_schema_are_enforced(self):
        bad = [[], {}, {**WIRE, "request_id": "forged"}, {**WIRE, "explanation": 3},
               {**WIRE, "proposals": {}}, {**WIRE, "proposals": ["not object"]}]
        for fields in ({"body": {}}, {"proposal_id": "prop_Forged00001"}, {"body_json": {}}, {"body_json": "oops"},
                       {"body_json": "[]"}, {"body_json": '{"reason":""}'}, {"rationale": ""}, {"kind": "invalid"},
                       {"cites": ["invalid"]}, {"body_json": '{"reason":NaN}'}, {"body_json": "[" * 2000 + "]" * 2000},
                       {"body_json": '{"reason":"quiet","reason":"duplicate"}'}):
            bad.append({**WIRE, "proposals": [{**WIRE["proposals"][0], **fields}]})
        for data in bad:
            with self.subTest(data=data):
                p, runner = provider(fake.reply_text(json.dumps(data)), fake.reply([]))
                self.assertEqual(p.propose(request())["proposals"], [])
                self.assertEqual(len(runner.calls), 2)

    def test_typed_errors_never_retry_or_leak_diagnostics(self):
        cases = [(fake.auth_error(), ProviderAuthError), (fake.rate_limited(), ProviderLimitError),
                 (fake.overloaded(), ProviderUnavailable), (fake.hang(), ProviderTimeout),
                 (fake.docker_failed(), ProviderUnavailable),
                 (fake.error(None, "secret-whatever"), ProviderUnavailable),
                 (RunResult(1, stderr="secret-token unauthorized"), ProviderAuthError)]
        for result, expected in cases:
            with self.subTest(expected=expected):
                p, runner = provider(result)
                with self.assertRaises(expected) as caught:
                    p.propose(request())
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(len(runner.calls), 1)

    def test_runner_exceptions_including_budget_are_not_wrapped(self):
        for failure in (SessionLimitReached("calls", "budget"), ProviderAuthError("auth"), ProviderLimitError("limit"),
                        ProviderTimeout("timeout"), ProviderUnavailable("offline")):
            def runner(*args):
                raise failure
            p = CodexProvider(runner, CodexSandboxConfig())
            with self.assertRaises(type(failure)) as caught:
                p.propose(request())
            self.assertIs(caught.exception, failure)
            self.assertEqual(p.last_calls, 0)

    def test_tool_result_is_shared_response_without_untrusted_proposals(self):
        for result in (fake.tool_attempt([NOTE]), fake.tool_attempt()):
            p, runner = provider(result)
            response = p.propose(request())
            self.assertEqual(jsonschema.errors("response", response), [])
            self.assertTrue(response["tool_use_detected"])
            self.assertEqual(response["proposals"], [])
            self.assertEqual(response["explanation"], "")
            self.assertEqual(len(runner.calls), 1)

    def test_one_model_explicit_configuration_and_options_validate(self):
        p, runner = provider(fake.reply([]), model="gpt-6-luna-custom")
        self.assertEqual(p.propose(request())["provider"]["model"], "gpt-6-luna-custom")
        for options in ({"timeout_s": 0}, {"timeout_s": -1}, {"timeout_s": float("inf")},
                        {"timeout_s": "1"}, {"max_format_retries": 2}, {"max_format_retries": -1},
                        {"max_format_retries": True}, {"model": "--evil"}):
            with self.assertRaises(ValueError):
                provider(**options)

    def test_total_deadline_includes_auth_retry_and_execution(self):
        p, runner = provider(fake.garbage(), fake.reply([]), timeout_s=30)
        with patch("iam.providers.codex.provider.time.monotonic", side_effect=[100, 101, 110, 112, 115, 116]):
            p.propose(request())
        self.assertEqual([call["timeout_s"] for call in runner.calls], [29, 18])
        p, runner = provider(fake.garbage(), fake.reply([]))
        with patch("iam.providers.codex.provider.time.monotonic", side_effect=[100, 101, 160, 161]):
            with self.assertRaises(ProviderTimeout):
                p.propose(request())
        self.assertEqual(len(runner.calls), 1)
        p, runner = provider(fake.reply([]))
        with patch("iam.providers.codex.provider.time.monotonic", side_effect=[100, 101, 161]):
            with self.assertRaises(ProviderTimeout):
                p.propose(request())
        p, runner = provider(fake.reply([]))
        with self.assertRaises(ProviderTimeout):
            p.propose(request(limits={"deadline_ms": 0, "max_output_tokens": 500}))
        self.assertEqual(runner.calls, [])

    def test_real_runner_requires_auth(self):
        p = CodexProvider(subprocess_runner, CodexSandboxConfig())
        with self.assertRaises(ProviderAuthError):
            p.propose(request())


class ClassifierTests(unittest.TestCase):
    def test_success_and_passive_item_lifecycle(self):
        self.assertEqual(classify(fake.reply([])).kind, Kind.OK)
        result = sequence({"type": "item.started", "item": {"id": "r", "type": "reasoning", "status": "in_progress"}},
                          {"type": "item.updated", "item": {"id": "r", "type": "reasoning"}},
                          {"type": "item.completed", "item": {"id": "r", "type": "reasoning", "status": "completed"}}, item())
        self.assertEqual(classify(result).kind, Kind.OK)
        self.assertEqual(classify(RunResult(0, "\n  \n" + fake.reply([]).stdout)).kind, Kind.OK)

    def test_tools_dominate_errors_nonzero_timeout_and_bad_lines(self):
        for kind in ("command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list", "future_tool"):
            for tail in ("", "\nnot JSON", '\n{"type":"turn.failed","error":{"message":"failed"}}'):
                result = sequence(item(kind))
                run = RunResult(127, result.stdout + tail, "secret auth error", timed_out=True)
                self.assertEqual(classify(run).kind, Kind.TOOL_USE)
        self.assertEqual(classify(fake.events({"type": "tool.started"})).kind, Kind.TOOL_USE)

    def test_malformed_output_duplicate_keys_constants_nonobjects(self):
        for text in ('{"type":null}', '{"type":1}', "{", "[]", "null", '{"type":"thread.started","type":"turn.started"}', '{"x":NaN}', '{"x":Infinity}'):
            self.assertEqual(classify(RunResult(0, text)).kind, Kind.FORMAT)
        for text in ("oops", "[]", "null", "[" * 2000 + "]" * 2000, '{"proposals":[],"proposals":[]}'):
            self.assertEqual(classify(fake.reply_text(text)).kind, Kind.FORMAT)

    def test_failures_even_with_valid_final_are_never_accepted(self):
        good = fake.reply([]).stdout
        for event in ({"type": "error", "message": "401 secret"},
                      {"type": "turn.failed", "error": {"message": "429 quota"}},
                      {"type": "turn.interrupted"}):
            result = classify(RunResult(0, good + json.dumps(event)))
            self.assertIn(result.kind, (Kind.AUTH, Kind.LIMIT, Kind.UNAVAILABLE))
            self.assertIsNone(result.data)
            self.assertNotIn("secret", result.message)
        self.assertEqual(classify(RunResult(1, good)).kind, Kind.UNAVAILABLE)
        self.assertEqual(classify(RunResult(0, good, "unexpected warning")).kind, Kind.UNAVAILABLE)
        self.assertEqual(classify(RunResult(0, good, timed_out=True)).kind, Kind.TIMEOUT)

    def test_incomplete_invalid_or_multiple_turns_fail_closed(self):
        cases = [fake.events(), fake.events({"type": "thread.started", "thread_id": "t"}),
                 sequence(item(), finish=False), fake.events({"type": "turn.completed"}),
                 sequence(item(), {"type": "turn.started"}), sequence(item(), {"type": "future.event"}),
                 fake.events({"type": "thread.started", "thread_id": ""}),
                 fake.events({"type": "thread.started", "thread_id": 1}),
                 sequence({"type": "item.completed", "item": None}),
                 sequence({"type": "item.completed"}),
                 sequence(item(id="")), sequence(item(id=1)), sequence(item(), item()),
                 sequence({"type": "item.started", "item": {"id": "r", "type": "reasoning"}}, item()),
                 sequence(item(status="failed")), sequence(item(status="in_progress")), sequence(item(error="failed")),
                 sequence({"type": "item.started", "item": {"id": "r", "type": "reasoning", "status": "completed"}}),
                 sequence(item(text=3)), sequence(item(phase="commentary")), sequence(),
                 sequence(item(), item(id="item_1")),
                 fake.events({"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}, item(),
                             {"type": "turn.completed", "status": "interrupted"}),
                 fake.events({"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}, item(),
                             {"type": "turn.completed", "error": "secret"})]
        for result in cases:
            with self.subTest(stdout=result.stdout):
                self.assertNotEqual(classify(result).kind, Kind.OK)
                self.assertIsNone(classify(result).data)


class CommandTests(unittest.TestCase):
    def test_isolation_no_mounts_secrets_or_host_services(self):
        command = docker_run(CodexSandboxConfig(), name="iam-codex-TEST")
        argv = list(command.argv)
        self.assertEqual(command.kill_argv(), ("docker", "kill", "iam-codex-TEST"))
        for flag, value in (("--platform", "linux/arm64"), ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                            ("--user", "10001:10001"), ("--network", "iam-sandbox"), ("--memory", "1g"),
                            ("--memory-swap", "1g"), ("--pids-limit", "128"), ("--pull", "never"),
                            ("--workdir", "/work"), ("--model", "gpt-6-luna"), ("--sandbox", "read-only")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        for flag in ("--read-only", "--rm", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
            self.assertIn(flag, argv)
        for flag in ("-v", "--mount", "--env-file", "--privileged", "--pid", "--ipc", "--device"):
            self.assertNotIn(flag, argv)
        mounts = [argv[i + 1] for i, value in enumerate(argv) if value == "--tmpfs"]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(all("noexec,nosuid,nodev" in mount and "uid=10001,gid=10001,mode=0700" in mount for mount in mounts))
        env = [argv[i + 1] for i, value in enumerate(argv) if value == "-e"]
        self.assertEqual(set(e.split("=", 1)[0] for e in env),
                         {"HOME", "CODEX_HOME", "HTTP_PROXY", "HTTPS_PROXY", "WS_PROXY", "WSS_PROXY", "http_proxy", "https_proxy"})
        self.assertIn("HOME=/home/codex", env)
        self.assertIn("CODEX_HOME=/home/codex/.codex", env)
        self.assertEqual(argv[-1], "-")
        self.assertIn("node", argv)
        self.assertIn("/opt/iam/launch.cjs", argv)
        self.assertIn("/opt/iam/response.schema.json", argv)
        self.assertIn("features.hooks=false", argv)
        self.assertIn("features.shell_tool=false", argv)
        self.assertIn("features.multi_agent=false", argv)
        self.assertRegex(docker_run(CodexSandboxConfig()).name, r"^iam-codex-[a-f0-9]{12}$")
        diagnostic = docker_run(CodexSandboxConfig(), command=["node", "-e", "0"])
        self.assertEqual(diagnostic.argv[-3:], ("node", "-e", "0"))
        self.assertNotIn("/opt/iam/launch.cjs", diagnostic.argv)

    def test_config_bounds_cannot_disable_or_expand_isolation(self):
        invalid = [{"image": "--evil"}, {"network": "host"}, {"network": "iam-sandbox\n"},
                   {"proxy_url": "https://host:80"}, {"proxy_url": "http://host:99999"},
                   {"proxy_url": "http://host:00"}, {"user": "0:0"}, {"platform": "linux/amd64"},
                   {"cpus": "0"}, {"cpus": "-1"}, {"cpus": "NaN"}, {"cpus": "5"},
                   {"memory": "0m"}, {"memory": "5g"}, {"pids": 0}, {"pids": 513}, {"pids": True},
                   {"tmp_size": "513m"}, {"home_size": "0m"}, {"work_size": "1g"}]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                CodexSandboxConfig(**options)
        CodexSandboxConfig(cpus="4", memory="4g", pids=512, tmp_size="512m", home_size="1m", work_size="512m")
        self.assertTrue(_size("512m", 512))
        self.assertFalse(_size("1g", 512))
        for options in ({"model": "--evil"}, {"name": "other-name"}):
            with self.assertRaises(ValueError):
                docker_run(CodexSandboxConfig(), **options)

    def test_wire_schema_is_strict_and_has_all_shared_kinds(self):
        self.assertFalse(RESPONSE_JSON_SCHEMA["additionalProperties"])
        proposal = RESPONSE_JSON_SCHEMA["properties"]["proposals"]["items"]
        self.assertFalse(proposal["additionalProperties"])
        self.assertEqual(set(proposal["required"]), set(proposal["properties"]))
        self.assertEqual(set(RESPONSE_JSON_SCHEMA["required"]), set(RESPONSE_JSON_SCHEMA["properties"]))
        self.assertEqual(set(proposal["properties"]["kind"]["enum"]),
                         {"action", "message", "state_change", "memory_write", "intention", "creator_request", "no_action", "attend"})
        self.assertEqual(proposal["properties"]["body_json"], {"type": "string"})


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "auth.json"
        self.write(AUTH)

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.path.chmod(0o600)

    def test_minimal_auth_projection_stdin_only_and_reload_per_attempt(self):
        self.write({**AUTH, "last_refresh": "ignored", "OPENAI_API_KEY": None})
        config = CodexSandboxConfig(auth_file=str(self.path))
        self.assertEqual(read_auth(str(self.path), REPO_ROOT), AUTH)
        self.assertEqual(read_auth(None, REPO_ROOT), {})
        runner = FakeCodex([fake.garbage(), fake.reply([])])
        calls = []
        def rotating(*args):
            calls.append(json.loads(args[1]))
            result = runner(*args)
            updated = {**AUTH, "tokens": {**AUTH["tokens"], "access_token": "rotated-secret"}}
            self.write(updated)
            return result
        p = CodexProvider(rotating, config)
        p.propose(request())
        self.assertEqual(calls[0]["auth"], AUTH)
        self.assertEqual(calls[1]["auth"]["tokens"]["access_token"], "rotated-secret")
        for call in runner.calls:
            for secret in (str(self.path), "secret-access", "rotated-secret", "secret-refresh", "secret-id"):
                self.assertNotIn(secret, str(call["argv"]))
                self.assertNotIn(secret, json.loads(call["stdin"])["prompt"])

    def test_auth_path_validation(self):
        self.assertEqual(validate_auth_path(str(self.path), REPO_ROOT), self.path)
        symlink = self.path.parent / "link.json"
        symlink.symlink_to(self.path)
        for value in ("relative.json", str(REPO_ROOT / "auth.json"), str(self.path.parent / ".." / "auth.json"),
                      str(symlink), str(self.path.parent), str(self.path.parent / "missing")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CodexSandboxConfig(auth_file=value)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            CodexSandboxConfig(auth_file=str(self.path))
        with self.assertRaises(ProviderAuthError) as caught:
            read_auth(str(self.path), REPO_ROOT)
        self.assertNotIn(str(self.path), str(caught.exception))

    def test_bad_or_api_credentials_fail_without_leaking(self):
        cases = [None, [], {**AUTH, "auth_mode": "apikey"}, {**AUTH, "OPENAI_API_KEY": "secret-api"},
                 {**AUTH, "api_key": "secret-api"}, {**AUTH, "tokens": None},
                 {**AUTH, "tokens": {**AUTH["tokens"], "api_key": "secret-api"}}]
        for key in ("access_token", "id_token", "refresh_token", "account_id"):
            for value in (None, 1, " "):
                cases.append({**AUTH, "tokens": {**AUTH["tokens"], key: value}})
        for value in cases:
            self.write(value)
            with self.subTest(value=value), self.assertRaises(ProviderAuthError) as caught:
                read_auth(str(self.path), REPO_ROOT)
            self.assertNotIn("secret-api", str(caught.exception))
        self.write({**AUTH, "tokens": {k: v for k, v in AUTH["tokens"].items() if k != "account_id"}})
        self.assertNotIn("account_id", read_auth(str(self.path), REPO_ROOT)["tokens"])
        for content in ("not JSON", "\xff", "x" * 65537, "[" * 2000 + "]" * 2000):
            self.path.write_bytes(content.encode("latin-1"))
            with self.assertRaises(ProviderAuthError):
                read_auth(str(self.path), REPO_ROOT)

    def test_auth_changed_since_config_or_open_is_rejected(self):
        config = CodexSandboxConfig(auth_file=str(self.path))
        self.path.unlink()
        p = CodexProvider(FakeCodex([fake.reply([])]), config)
        with self.assertRaises(ProviderAuthError):
            p.propose(request())
        self.write(AUTH)
        real_fstat = os.fstat
        def changed_stat(fd):
            result = real_fstat(fd)
            values = list(result)
            values[0] = stat.S_IFREG | 0o644
            return os.stat_result(values)
        with patch("iam.providers.codex.auth.os.fstat", changed_stat), self.assertRaises(ProviderAuthError):
            read_auth(str(self.path), REPO_ROOT)
        with patch("iam.providers.codex.auth.os.open", side_effect=OSError), self.assertRaises(ProviderAuthError):
            read_auth(str(self.path), REPO_ROOT)


class FakeTests(unittest.TestCase):
    def test_queue_exhaustion_cleanup_optional(self):
        runner = FakeCodex().queue(fake.hang())
        self.assertTrue(runner(["cmd"], "", 1).timed_out)
        self.assertEqual(runner.calls[0]["cleanup"], [])
        with self.assertRaises(AssertionError):
            runner(["cmd"], "", 1)
        self.assertEqual(len(runner.calls), 2)


if __name__ == "__main__":
    unittest.main()
