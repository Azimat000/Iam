"""Тесты измерителя покрытия tools/covcheck.py (S04, R-223): пороги по модулям на искусственных данных,
код возврата, полный прогон на крошечном пробном проекте."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "covcheck.py"
spec = importlib.util.spec_from_file_location("covcheck", TOOL)
covcheck = importlib.util.module_from_spec(spec)
sys.modules["covcheck"] = covcheck  # dataclass ищет свой модуль в sys.modules
spec.loader.exec_module(covcheck)

PYPROJECT = """
[tool.coverage.run]
branch = true
source = ["src", "tools"]
[tool.iam.coverage]
min_lines = 90.0
critical_min_lines = 98.0
critical_min_branches = 100.0
critical = ["src/crit/*.py"]
"""


def summary(stmts, covered, branches=0, covered_br=0, missing=()):
    return {"summary": {"num_statements": stmts, "covered_lines": covered, "num_branches": branches,
                        "covered_branches": covered_br}, "missing_lines": list(missing)}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="iam-cov-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
        for rel in ("src/a.py", "src/crit/b.py", "tools/c.py"):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x = 1\n", encoding="utf-8")
        self.cfg = covcheck.load_config(self.root / "pyproject.toml")

    def run_tool(self, *args):
        env = dict(os.environ, IAM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env)


class EvaluateTest(Base):
    def test_thresholds_per_module(self):
        report = {"files": {
            "src/a.py": summary(100, 90),                                   # ровно 90 % — проходит
            str(self.root / "src/crit/b.py"): summary(100, 97, 10, 10, [5]),  # критичный, строк 97 % — нет
            "tools/c.py": summary(100, 89),                                 # 89 % — нет
        }}
        res = {r.path: r for r in covcheck.evaluate(report, self.cfg, self.root)}
        self.assertTrue(res["src/a.py"].ok)
        self.assertFalse(res["src/a.py"].critical)
        self.assertTrue(res["src/crit/b.py"].critical)
        self.assertIn("строк 97.0 % < 98 %", res["src/crit/b.py"].problems[0])
        self.assertFalse(res["tools/c.py"].ok)
        text = covcheck.render(list(res.values()))
        self.assertIn("ниже порога: 2", text)
        self.assertIn("не покрыты строки: 5", text)

    def test_critical_requires_all_branches(self):
        report = {"files": {"src/a.py": summary(10, 10), "tools/c.py": summary(0, 0),
                            "src/crit/b.py": summary(100, 100, 10, 9)}}
        res = {r.path: r for r in covcheck.evaluate(report, self.cfg, self.root)}
        self.assertEqual(res["src/crit/b.py"].problems, ("ветвей 90.0 % < 100 %",))
        self.assertTrue(res["tools/c.py"].ok, "модуль без операторов считается покрытым")

    def test_unloaded_module_is_zero_and_pragma_forbidden(self):
        (self.root / "src/a.py").write_text("x = 1  # " + covcheck.PRAGMA + "\n", encoding="utf-8")
        report = {"files": {"src/a.py": summary(1, 1), "src/crit/b.py": summary(1, 1)}}
        res = {r.path: r for r in covcheck.evaluate(report, self.cfg, self.root)}
        self.assertIn("запрещена", res["src/a.py"].problems[0])
        self.assertEqual(res["tools/c.py"].lines_pct, 0.0)
        self.assertIn("ни разу", res["tools/c.py"].problems[0])

    def test_exit_codes_on_json(self):
        good = {"files": {f: summary(10, 10) for f in ("src/a.py", "src/crit/b.py", "tools/c.py")}}
        bad = {"files": {**good["files"], "tools/c.py": summary(10, 5)}}
        for report, code in ((good, 0), (bad, 1)):
            path = self.root / "r.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            p = self.run_tool("--json", str(path))
            self.assertEqual(p.returncode, code, p.stdout + p.stderr)

    def test_bad_config(self):
        (self.root / "pyproject.toml").write_text("[tool.iam]\n", encoding="utf-8")
        p = self.run_tool("--json", "x.json")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("tool.iam.coverage", p.stderr)

    def test_missing_coverage_package(self):
        with mock.patch.object(covcheck.importlib.util, "find_spec", return_value=None), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(covcheck.main([]), 2)
        self.assertIn("coverage", "".join(c.args[0] for c in err.write.call_args_list))


@unittest.skipIf(importlib.util.find_spec("coverage") is None, "coverage.py не установлен")
class FullRunTest(Base):
    """Настоящий прогон на пробном проекте: тесты под coverage → отчёт → пороги."""

    def make_project(self, test_body: str):
        shutil.rmtree(self.root / "tools")
        (self.root / "src/crit/b.py").unlink()
        (self.root / "src/a.py").write_text(textwrap.dedent("""\
            def f(x):
                if x:
                    return 1
                return 2
            """), encoding="utf-8")
        (self.root / "tests").mkdir()
        body = "".join(" " * 8 + line + "\n" for line in test_body.strip().splitlines())
        (self.root / "tests/test_a.py").write_text(
            "import sys, unittest\nsys.path.insert(0, 'src')\nimport a\n\n\n"
            "class T(unittest.TestCase):\n    def test(self):\n" + body, encoding="utf-8")

    def test_full_run_passes(self):
        self.make_project("self.assertEqual(a.f(1), 1)\nself.assertEqual(a.f(0), 2)\n")
        p = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("src/a.py", p.stdout)

    def test_full_run_below_threshold(self):
        self.make_project("self.assertEqual(a.f(1), 1)\n")
        p = self.run_tool()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("НИЖЕ ПОРОГА", p.stdout)

    def test_failing_tests_invalidate_measurement(self):
        self.make_project("self.assertEqual(a.f(1), 1)\nself.assertEqual(a.f(0), 2)\nself.fail('упал')\n")
        p = self.run_tool()
        self.assertEqual(p.returncode, 1)
        self.assertIn("Тесты упали", p.stdout)


if __name__ == "__main__":
    unittest.main()
