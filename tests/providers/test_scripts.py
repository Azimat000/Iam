"""Скрипты проверки на Mac (G11): `bash -n`, сборка отчёта машинно, ход скрипта на подделке docker, артефакты docker/."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "claude" / "verify_isolation.sh"
SHIM = Path(__file__).resolve().parent / "shim_docker.py"
TOKEN = "sk" + "-ant-oat01-CanaryTokenValue0123456789abcdefghij"   # собрано из частей: репозиторий не должен содержать токеноподобных строк


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


render_report = load("render_report", ROOT / "scripts" / "claude" / "render_report.py")
emit_cmd = load("emit_cmd", ROOT / "scripts" / "claude" / "emit_cmd.py")


class StaticTest(unittest.TestCase):
    def test_shell_syntax(self):
        done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_script_has_the_safety_checks_it_promises(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for needle in ("права файла токена должны быть 600", "не должен лежать в репозитории", "CANARY", "--noproxy",
                       "render_report.py", "set -u", "network inspect"):
            self.assertIn(needle, text)
        self.assertTrue(os.access(SCRIPT, os.R_OK))

    def test_every_required_check_is_produced_by_the_script(self):
        text = SCRIPT.read_text(encoding="utf-8")
        shown = set(re.findall(r"record (\w+) (?:PASS|FAIL)", text)) | set(re.findall(r'rec\("(\w+)", "(?:PASS|FAIL)"', text))
        self.assertEqual(set(render_report.REQUIRED) - shown, set())

    def test_emit_cmd_prints_the_same_isolation_flags(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(emit_cmd.main(["--token-file", "/Users/me/t.env", "--", "sh", "-c", "id -u"]), 0)
        line = buf.getvalue()
        for flag in ("--read-only", "--cap-drop ALL", "--env-file /Users/me/t.env", "iam-claude:1 sh -c 'id -u'"):
            self.assertIn(flag, line)
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_cmd.main(["--claude", "--model", "opus"])
        self.assertIn("claude -p", buf.getvalue())
        self.assertIn("--model opus", buf.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            emit_cmd.main([])


class ReportTest(unittest.TestCase):
    def all_pass(self):
        return {i: {"id": i, "status": "PASS", "detail": "ок"} for i in render_report.REQUIRED}

    def template(self):
        return (ROOT / "docs" / "evidence" / "claude-isolation.template.md").read_text(encoding="utf-8")

    def test_all_required_passing_is_a_pass(self):
        ok, problems = render_report.verdict(self.all_pass())
        self.assertEqual((ok, problems), (True, []))
        text = render_report.render(self.template(), self.all_pass(), when="2026-10-16T12:00Z", host="Darwin arm64")
        self.assertIn("## Итог: ПРОЙДЕНА", text)
        self.assertNotIn("{{", text)
        self.assertEqual(text.count("| PASS |"), len(render_report.REQUIRED))

    def test_missing_failed_and_skipped_required_checks_fail_the_report(self):
        for mutate, expected in ((lambda r: r.pop("auth"), "auth: нет результата"),
                                 (lambda r: r["net_denied"].update(status="FAIL"), "net_denied: FAIL"),
                                 (lambda r: r["format"].update(status="SKIP"), "format: SKIP")):
            results = self.all_pass()
            mutate(results)
            ok, problems = render_report.verdict(results)
            self.assertFalse(ok)
            self.assertIn(expected, problems)
            self.assertIn("НЕ ПРОЙДЕНА", render_report.render(self.template(), results, when="t", host="h"))

    def test_extra_failure_also_fails_but_extra_pass_is_listed(self):
        results = self.all_pass()
        results["bonus"] = {"id": "bonus", "status": "PASS", "detail": "x"}
        self.assertTrue(render_report.verdict(results)[0])
        self.assertIn("`bonus`", render_report.render(self.template(), results, when="t", host="h"))
        results["bonus"]["status"] = "FAIL"
        self.assertFalse(render_report.verdict(results)[0])

    def test_secrets_never_reach_the_report_and_cells_are_escaped(self):
        results = self.all_pass()
        results["auth"]["detail"] = f"токен {TOKEN} | лишний\nстолбец"
        text = render_report.render(self.template(), results, when="t", host="h")
        self.assertNotIn(TOKEN, text)
        self.assertIn("[СЕКРЕТ УДАЛЁН]", text)
        self.assertNotIn("лишний\nстолбец", text)

    def test_reading_results(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.jsonl"
            p.write_text('{"id": "arch", "status": "PASS", "detail": "x"}\n\n{"id": "auth", "status": "FAIL"}\n', encoding="utf-8")
            self.assertEqual(sorted(render_report.read_results(p)), ["arch", "auth"])
            p.write_text('{"id": "arch", "status": "МАЯК"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                render_report.read_results(p)
            p.write_text('{"status": "PASS"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                render_report.read_results(p)
            p.write_text('{"id": "arch", "status": "FAIL"}\n{"id": "arch", "status": "PASS"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):          # повторная запись проверки не должна «перекрасить» провал
                render_report.read_results(p)

    def test_main_writes_the_report_and_returns_the_verdict(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        with tempfile.TemporaryDirectory() as d:
            results, report = Path(d) / "r.jsonl", Path(d) / "report.md"
            results.write_text("\n".join(json.dumps(r) for r in self.all_pass().values()), encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(render_report.main([str(results), str(report)]), 0)
            self.assertIn("ПРОЙДЕНА", out.getvalue())
            self.assertIn("## Итог: ПРОЙДЕНА", report.read_text(encoding="utf-8"))
            results.write_text(json.dumps({"id": "arch", "status": "FAIL", "detail": f"токен {TOKEN}"}), encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(render_report.main([str(results), str(report)]), 1)
            self.assertIn("НЕ ПРОЙДЕНА", out.getvalue())
            self.assertIn("секреты", err.getvalue())


@unittest.skipUnless(shutil.which("bash"), "нужен bash")
class ScriptFlowTest(unittest.TestCase):
    """Ход verify_isolation.sh на подделке docker: идеальный контейнер проходит, каждая дыра ловится своей проверкой."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="iam-s08-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        shim = bindir / "docker"
        shim.write_text(f"#!{sys.executable}\n" + SHIM.read_text(encoding="utf-8").split("\n", 1)[1], encoding="utf-8")
        shim.chmod(0o755)
        self.token = self.tmp / "secrets" / "claude.env"
        self.token.parent.mkdir()
        self.token.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n", encoding="utf-8")
        self.token.chmod(0o600)
        self.bindir = bindir

    def run_script(self, bad="", token=None, **env):
        e = {**os.environ, "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}", "SHIM_BAD": bad,
             "IAM_CLAUDE_TOKEN_FILE": str(token or self.token), "IAM_VERIFY_OUT": str(self.tmp / "out"),
             "IAM_VERIFY_REPORT": str(self.tmp / "report.md"), **env}
        return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=e, timeout=120)

    def results(self):
        lines = (self.tmp / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines()
        return {r["id"]: r for r in map(json.loads, lines)}

    def test_ideal_container_passes_everything(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        results = self.results()
        self.assertEqual({i: r["status"] for i, r in results.items()}, {i: "PASS" for i in render_report.REQUIRED})
        self.assertIn("## Итог: ПРОЙДЕНА", (self.tmp / "report.md").read_text(encoding="utf-8"))

    def test_every_hole_is_caught_by_its_own_check(self):
        for check in render_report.REQUIRED:
            with self.subTest(check=check):
                done = self.run_script(bad=check)
                self.assertEqual(done.returncode, 1, (check, done.stdout[-800:]))
                results = self.results()
                self.assertEqual(results[check]["status"], "FAIL", check)
                others = [i for i, r in results.items() if r["status"] == "FAIL" and i != check]
                # без входа запрос со списком инструментов тоже не удаётся — это следствие, а не отдельная дыра
                self.assertEqual(others, ["no_tools"] if check == "auth" else [], f"{check}: лишние провалы {others}")
                self.assertIn("НЕ ПРОЙДЕНА", (self.tmp / "report.md").read_text(encoding="utf-8"))

    def test_dead_docker_gives_no_false_pass(self):
        """Контейнер не запускается вовсе: ни одна проверка устройства не должна показать PASS (ложное «секретов нет»)."""
        done = self.run_script(SHIM_DEAD="1")
        self.assertEqual(done.returncode, 1)
        results = self.results()
        passed = [i for i, r in results.items() if r["status"] == "PASS"]
        self.assertEqual(passed, [])
        for check in ("arch", "non_root", "no_mounts", "canary_file", "canary_env", "no_docker_socket", "read_only_root",
                      "no_capabilities", "limits", "net_allowed", "net_denied", "net_direct_denied", "sandbox_internal"):
            self.assertEqual(results[check]["status"], "FAIL", check)

    def test_missing_init_event_is_a_failure_not_a_pass(self):
        done = self.run_script(bad="no_init")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.results()["no_tools"]["status"], "FAIL")
        self.assertIn("init", self.results()["no_tools"]["detail"])

    def test_token_file_must_hold_exactly_the_token_line(self):
        extra = self.tmp / "extra.env"
        extra.write_text("CLAUDE_CODE_OAUTH_TOKEN=x\nANTHROPIC_BASE_URL=https://evil.example\n")
        extra.chmod(0o600)
        wrong = self.tmp / "wrong.env"
        wrong.write_text("NODE_OPTIONS=--require=/x.js\n")
        wrong.chmod(0o600)
        for path in (extra, wrong):
            done = self.run_script(token=path)
            self.assertEqual(done.returncode, 2)
            self.assertIn("ровно одна строка", done.stdout)

    def test_token_never_appears_in_outputs_or_report(self):
        done = self.run_script(bad="auth")
        blob = done.stdout + done.stderr + (self.tmp / "report.md").read_text(encoding="utf-8")
        for p in (self.tmp / "out").rglob("*"):
            if p.is_file():
                blob += p.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn(TOKEN, blob)

    def test_refuses_to_run_with_unsafe_inputs(self):
        loose = self.tmp / "loose.env"
        loose.write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n")
        loose.chmod(0o644)
        for token, text in ((loose, "600"), (self.tmp / "нет.env", "IAM_CLAUDE_TOKEN_FILE")):
            done = self.run_script(token=token)
            self.assertEqual(done.returncode, 2)
            self.assertIn(text, done.stdout)
        inside = ROOT / "tests" / "providers" / "_tmp_inside.env"
        inside.write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n")
        inside.chmod(0o600)
        self.addCleanup(inside.unlink, missing_ok=True)
        done = self.run_script(token=inside)
        self.assertEqual(done.returncode, 2)
        self.assertIn("репозитории", done.stdout)
        self.assertFalse((self.tmp / "report.md").exists())

    def test_no_docker_or_dead_daemon_is_a_clear_error(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        env = {**os.environ, "PATH": f"{empty}{os.pathsep}/usr/bin{os.pathsep}/bin", "IAM_CLAUDE_TOKEN_FILE": str(self.token)}
        done = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)
        if shutil.which("docker", path="/usr/bin:/bin") is None:
            self.assertEqual(done.returncode, 2)
            self.assertIn("нет docker", done.stdout)


class ArtifactsTest(unittest.TestCase):
    """Статические проверки docker/: не root, без секретов, посредник только с белым списком."""

    def test_dockerfile(self):
        text = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("--platform=linux/arm64", text)
        self.assertRegex(text, r"USER 10001:10001")
        self.assertLess(text.index("useradd"), text.index("USER 10001"))
        self.assertRegex(text, r"ARG CLAUDE_CODE_VERSION=\d+\.\d+\.\d+")
        for bad in ("OAUTH", "TOKEN=", "ANTHROPIC_API_KEY", "COPY ", "ADD ", "docker.sock", "VOLUME"):
            self.assertNotIn(bad, text.replace("DISABLE_TELEMETRY", ""), bad)

    def test_proxy_allowlist_is_minimal_and_ends_with_deny_all(self):
        text = (ROOT / "docker" / "squid.conf").read_text(encoding="utf-8")
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
        hosts = [l for l in lines if l.startswith("acl allowed_hosts")]
        self.assertEqual(hosts, ["acl allowed_hosts dstdomain api.anthropic.com platform.claude.com"])
        rules = [l for l in lines if l.startswith("http_access")]
        self.assertEqual(rules[-1], "http_access deny all")
        self.assertEqual([r for r in rules if r.startswith("http_access allow")], ["http_access allow CONNECT allowed_hosts"])
        self.assertIn("cache deny all", lines)

    def test_compose_keeps_the_sandbox_network_internal(self):
        text = (ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
        self.assertRegex(text, r"iam-sandbox:\n\s+name: iam-sandbox\n\s+internal: true")
        self.assertIn("read_only: true", text)
        self.assertIn("cap_drop: [ALL]", text)
        self.assertNotIn("privileged", text)
        self.assertNotIn("docker.sock", text)

    def test_default_config_matches_the_compose_names(self):
        from iam.providers import SandboxConfig
        cfg = SandboxConfig()
        compose = (ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn(f"name: {cfg.network}", compose)
        self.assertIn("container_name: iam-proxy", compose)
        self.assertIn("iam-proxy", cfg.proxy_url)
        self.assertIn(f"-t {cfg.image}", (ROOT / "docker" / "README.md").read_text(encoding="utf-8"))
        self.assertIn(f"USER {cfg.user}", (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
