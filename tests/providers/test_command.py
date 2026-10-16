"""Сборщик `docker run`: каждый флаг изоляции на месте, запрещённого нет (R-110…R-115, R-149)."""
import shutil
import subprocess
import unittest

from iam.providers import FIXED_ENV, SandboxConfig, docker_run

TOKEN_FILE = "/Users/me/.iam-secrets/claude.env"


class IsolationFlagsTest(unittest.TestCase):
    def setUp(self):
        self.cmd = docker_run(SandboxConfig(token_file=TOKEN_FILE), model="sonnet")
        self.argv = list(self.cmd.argv)

    def value(self, flag):
        i = self.argv.index(flag)
        return self.argv[i + 1]

    def test_every_isolation_flag_is_present(self):
        a = self.argv
        self.assertEqual(a[:3], ["docker", "run", "--rm"])
        self.assertIn("-i", a)                                        # контекст через stdin
        self.assertIn("--read-only", a)
        self.assertEqual(self.value("--cap-drop"), "ALL")
        self.assertEqual(self.value("--security-opt"), "no-new-privileges")
        self.assertEqual(self.value("--platform"), "linux/arm64")
        self.assertEqual(self.value("--user"), "10001:10001")
        self.assertEqual(self.value("--pids-limit"), "128")
        self.assertEqual(self.value("--memory"), "1g")
        self.assertEqual(self.value("--memory-swap"), "1g")           # без своппинга
        self.assertEqual(self.value("--cpus"), "1.0")
        self.assertEqual(self.value("--network"), "iam-sandbox")
        tmpfs = [a[i + 1] for i, x in enumerate(a) if x == "--tmpfs"]
        self.assertEqual(len(tmpfs), 2)
        self.assertTrue(all("noexec" in t and "nosuid" in t and "size=" in t for t in tmpfs))
        self.assertIn(self.cmd.name, a)

    def test_forbidden_things_are_absent(self):
        docker_part = self.argv[:self.argv.index("iam-claude:1")]   # дальше — команда claude внутри контейнера
        text = " ".join(docker_part).replace(TOKEN_FILE, "<токен-файл>")   # путь к токену разрешён один раз, ниже проверим
        self.argv = docker_part
        for flag in ("-v", "--volume", "--mount", "--privileged", "--pid", "--ipc", "--uts", "--device", "--cap-add",
                     "--userns", "--add-host", "-p", "--publish", "--publish-all", "--network-alias"):
            self.assertNotIn(flag, self.argv, flag)
        for word in ("docker.sock", "/var/run", "/Users", "/home/user", "host", "--security-opt=seccomp=unconfined",
                     "apparmor=unconfined", "TELEGRAM", "ANTHROPIC_API_KEY", "ssh", ".env"):
            if word == ".env":    # единственный файл окружения — токен подписки, и только он
                self.assertEqual([x for x in self.argv if x.endswith(".env")], [TOKEN_FILE])
                continue
            self.assertNotIn(word, text, word)

    def test_environment_is_an_allowlist(self):
        envs = [self.argv[i + 1].split("=", 1) for i, x in enumerate(self.argv) if x == "-e"]
        names = {k for k, _ in envs}
        allowed = set(FIXED_ENV) | {"HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"}
        self.assertEqual(names, allowed)
        self.assertEqual({v for k, v in envs if "PROXY" in k.upper()}, {"http://iam-proxy:3128"})
        self.assertEqual(dict(envs)["DISABLE_TELEMETRY"], "1")

    def test_token_goes_by_file_not_by_value(self):
        self.assertEqual(self.value("--env-file"), TOKEN_FILE)
        self.assertEqual(self.argv.count("--env-file"), 1)
        without = docker_run(SandboxConfig(), model="sonnet").argv
        self.assertNotIn("--env-file", without)

    def test_claude_runs_tool_less_and_stateless(self):
        a, i = self.argv, self.argv.index("claude")
        tail = a[i:]
        self.assertEqual(a[i - 1], "iam-claude:1")                     # образ непосредственно перед командой
        self.assertEqual(tail[:2], ["claude", "-p"])
        self.assertEqual(tail[tail.index("--tools") + 1], "")          # ни одного инструмента
        for flag in ("--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config"):
            self.assertIn(flag, tail)
        self.assertEqual(tail[tail.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(tail[tail.index("--permission-prompts") + 1], "none")
        self.assertEqual(tail[tail.index("--output-format") + 1], "json")
        self.assertEqual(tail[tail.index("--model") + 1], "sonnet")
        self.assertNotIn("--dangerously-skip-permissions", a)
        self.assertNotIn("--bare", a)   # --bare не читает вход подписки (документация)
        self.assertIn("--json-schema", tail)
        self.assertNotIn("--json-schema", docker_run(SandboxConfig(), model="opus", json_schema=None).argv)

    def test_names_are_unique_and_kill_command_targets_the_container(self):
        other = docker_run(SandboxConfig(), model="sonnet")
        self.assertNotEqual(self.cmd.name, other.name)
        self.assertEqual(self.cmd.kill_argv(), ("docker", "kill", self.cmd.name))
        self.assertEqual(docker_run(SandboxConfig(), model="sonnet", name="iam-claude-fixed").name, "iam-claude-fixed")


class ConfigGuardTest(unittest.TestCase):
    def test_unsafe_configurations_cannot_be_built(self):
        bad = [dict(network="host"), dict(network="bridge"), dict(network="none"), dict(network="container:abc"),
               dict(user="0:0"), dict(user="root"), dict(user="1000:0"), dict(memory="0g"), dict(memory="lots"),
               dict(cpus="all"), dict(pids=0), dict(pids=100000), dict(proxy_url="https://x:1"), dict(proxy_url="socks5://x:1"),
               dict(token_file="relative/token.env"), dict(token_file="/a/../etc/passwd"), dict(image="bad image"),
               dict(tmp_size="big"), dict(home_size="1"), dict(network="bad net")]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                SandboxConfig(**kw)

    def test_model_name_is_checked(self):
        with self.assertRaises(ValueError):
            docker_run(SandboxConfig(), model="sonnet; rm -rf /")

    @unittest.skipUnless(shutil.which("docker"), "нет клиента docker")
    def test_docker_client_knows_every_flag_we_use(self):
        """Клиент docker (без демона) разбирает наши флаги: опечатки в именах флагов ловятся без запуска контейнера."""
        help_text = subprocess.run(["docker", "run", "--help"], capture_output=True, text=True).stdout
        used = {x for x in docker_run(SandboxConfig(token_file=TOKEN_FILE), model="sonnet").argv[2:]
                if x.startswith("--") and x != "--tools"}
        docker_part = used - {"--output-format", "--no-session-persistence", "--permission-mode", "--permission-prompts",
                              "--disable-slash-commands", "--strict-mcp-config", "--model", "--system-prompt",
                              "--json-schema"}   # это флаги claude, не docker
        missing = sorted(f for f in docker_part if f not in help_text)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
