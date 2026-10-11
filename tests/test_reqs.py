"""Тесты tools/reqs.py на временной копии репозитория (IAM_ROOT) и на реальном реестре."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "reqs.py"

sys.path.insert(0, str(REPO / "tools"))
import reqs as reqs_tool  # noqa: E402

# Заготовка плана: для каждого раздела из reqs.SECTIONS — строка-заголовок и одна строка-тело.
# Заголовок раздела i (с 0) — строка 2*i+1, тело — строка 2*i+2.
KEYS = [k for k, _ in reqs_tool.SECTIONS]
PLAN = "\n".join(f"{prefix}х\nтело {key}" for key, prefix in reqs_tool.SECTIONS) + "\n"


def body(key):
    return 2 * KEYS.index(key) + 2


def heading(key):
    return 2 * KEYS.index(key) + 1


GOOD = {
    "id": "R-001", "source": "§1", "lines": str(body("§1")), "text": "т", "kind": "functional",
    "mechanism": "m", "check": "c", "slice": "T1", "status": "planned", "evidence": "",
}


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False)


def dump(reqs, skips=()):
    out = []
    for r in reqs:
        out.append("[[req]]")
        out += [f"{k} = {toml_value(v)}" for k, v in r.items()]
        out.append("")
    for s in skips:
        out.append("[[skip]]")
        out += [f"{k} = {toml_value(v)}" for k, v in s.items()]
        out.append("")
    return "\n".join(out)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "state.json").write_text(
            json.dumps({"slices": {"T1": {}, "T2": {}}}), encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def plan(self, text=PLAN):
        (self.root / "docs" / "PLAN.md").write_text(text, encoding="utf-8")

    def skips_except(self, reqs):
        """Пропуски для всех строк заготовки, не покрытых переданными требованиями."""
        covered = set()
        for r in reqs:
            if "lines" in r:
                covered |= reqs_tool.parse_lines(r["lines"], [], "")
        n = len(PLAN.splitlines())
        return [{"lines": str(i), "why": "тест"} for i in range(1, n + 1) if i not in covered]

    def write(self, reqs, skips=()):
        (self.root / "requirements.toml").write_text(dump(reqs, skips), encoding="utf-8")

    def run_tool(self, *args):
        env = dict(os.environ, IAM_ROOT=str(self.root))
        p = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env)
        return p.returncode, p.stdout + p.stderr


def mk(**kw):
    d = dict(GOOD)
    d.update(kw)
    return d


class ValidateTest(Base):
    def test_valid(self):
        self.plan()
        reqs = [mk(), mk(id="R-002", source="§2.1", lines=str(body("§2.1")))]
        self.write(reqs, self.skips_except(reqs))
        code, out = self.run_tool("validate")
        self.assertEqual(code, 0, out)
        self.assertIn("2 требований", out)

    def test_duplicate_id(self):
        self.write([mk(), mk()])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("id повторяется", out)

    def test_missing_field(self):
        r = mk()
        del r["check"]
        self.write([r])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("нет поля check", out)

    def test_empty_mechanism_and_check(self):
        self.write([mk(mechanism="  ", check="")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("поле mechanism пустое", out)
        self.assertIn("поле check пустое", out)

    def test_process_kind_still_needs_check(self):
        self.write([mk(kind="process", check="")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("поле check пустое", out)

    def test_bad_enum_values(self):
        self.write([mk(kind="weird", status="done")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("kind", out)
        self.assertIn("status", out)

    def test_unknown_slice(self):
        self.write([mk(slice="T9")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("срез «T9» не существует", out)

    def test_slice_list(self):
        self.write([mk(slice=["T1", "T2"])])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 0, out)

    def test_unknown_source(self):
        self.write([mk(source="§99")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("source «§99» неизвестен", out)

    def test_claude_md_source_without_lines(self):
        r = mk(source="CLAUDE.md")
        del r["lines"]
        self.write([r])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 0, out)

    def test_verified_requires_evidence(self):
        self.write([mk(status="verified", evidence="")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("verified без свидетельства", out)

    def test_verified_evidence_path_must_exist(self):
        self.write([mk(status="verified", evidence="tests/nope.py — прогон")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("не найдено в репозитории", out)

    def test_verified_with_existing_evidence(self):
        (self.root / "ev.txt").write_text("ok", encoding="utf-8")
        self.write([mk(status="verified", evidence="ev.txt — результат; ev.txt::часть")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 0, out)

    def test_evidence_path_escape_rejected(self):
        self.write([mk(status="verified", evidence="../x")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)

    def test_waived_needs_waiver(self):
        self.write([mk(status="waived")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("waiver", out)
        self.write([mk(status="waived", waiver="вне объёма по решению D-xxx")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 0, out)

    def test_bad_lines(self):
        self.write([mk(lines="abc")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("неверная запись строк", out)

    def test_lines_outside_section(self):
        self.plan()
        reqs = [mk(lines=str(body("§2.1")))]  # строка из §2.1, а source §1
        self.write(reqs, self.skips_except(reqs))
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("не в разделе §1", out)

    def test_uncovered_plan_line(self):
        self.plan()
        reqs = [mk()]
        skips = [x for x in self.skips_except(reqs) if x["lines"] != str(body("§3.4"))]
        self.write(reqs, skips)
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn(f"без требования и без пропуска: {body('§3.4')}", out)

    def test_missing_plan_heading(self):
        self.plan(PLAN.replace("### Правило выбора", "### Другое"))
        reqs = [mk()]
        self.write(reqs, self.skips_except(reqs))
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("§3.4", out)

    def test_skip_needs_reason(self):
        self.plan()
        reqs = [mk()]
        skips = self.skips_except(reqs)
        skips[0]["why"] = ""
        self.write(reqs, skips)
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("нет обоснования", out)

    def test_line_in_both_req_and_skip(self):
        self.plan()
        reqs = [mk()]
        skips = self.skips_except(reqs) + [{"lines": str(body("§1")), "why": "x"}]
        self.write(reqs, skips)
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("одновременно", out)

    def test_lines_beyond_plan(self):
        self.plan()
        self.write([mk(lines="9999")])
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)
        self.assertIn("за пределами PLAN.md", out)

    def test_empty_registry(self):
        (self.root / "requirements.toml").write_text("", encoding="utf-8")
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1)


class ReportTest(Base):
    def setUp(self):
        super().setUp()
        (self.root / "ev.txt").write_text("ok", encoding="utf-8")
        self.write([
            mk(),
            mk(id="R-002", slice="T2", status="implemented"),
            mk(id="R-003", status="verified", evidence="ev.txt"),
            mk(id="R-004", status="verified", evidence="ev.txt", stub_only=True),
            mk(id="R-005", status="waived", waiver="вне объёма"),
        ])

    def test_unverified_lists_only_open(self):
        code, out = self.run_tool("unverified")
        self.assertEqual(code, 0, out)
        self.assertIn("R-001", out)
        self.assertIn("R-002", out)
        self.assertNotIn("R-003", out)
        self.assertNotIn("R-004", out)
        self.assertNotIn("R-005", out)
        self.assertIn("без свидетельства: 2 из 5", out)

    def test_unverified_by_slice(self):
        code, out = self.run_tool("unverified", "--slice", "T2")
        self.assertIn("R-002", out)
        self.assertNotIn("R-001", out)

    def test_report_counts_and_stub(self):
        code, out = self.run_tool("report")
        self.assertEqual(code, 0, out)
        self.assertIn("требований 5: verified 2 (из них на заглушке 1), implemented 1, planned 1, waived 1", out)
        self.assertIn("T1: 4 / 1", out)
        self.assertIn("T2: 1 / 1", out)


class CoverageTest(Base):
    def test_write_and_check(self):
        self.plan()
        reqs = [mk()]
        self.write(reqs, self.skips_except(reqs))
        code, out = self.run_tool("coverage", "--write", "docs/spec/cov.md")
        self.assertEqual(code, 0, out)
        text = (self.root / "docs" / "spec" / "cov.md").read_text(encoding="utf-8")
        self.assertIn("| §1 |", text)
        self.assertIn("R-001", text)
        self.assertIn("## Явные пропуски", text)
        code, out = self.run_tool("coverage", "--check", "docs/spec/cov.md")
        self.assertEqual(code, 0, out)
        (self.root / "docs" / "spec" / "cov.md").write_text("старое", encoding="utf-8")
        code, out = self.run_tool("coverage", "--check", "docs/spec/cov.md")
        self.assertEqual(code, 1)
        self.assertIn("УСТАРЕЛО", out)

    def test_no_coverage_for_invalid_registry(self):
        self.write([mk(slice="T9")])
        code, out = self.run_tool("coverage", "--write", "x.md")
        self.assertEqual(code, 1)


class BriefTest(Base):
    """memory.py brief показывает сводку реестра, а без реестра работает как раньше."""

    def brief(self):
        state = {"project": "t", "current_slice": "T1", "revision": 1,
                 "slices": {"T1": {"title": "a", "stage": 1, "depends": [], "status": "todo"}},
                 "gates": [], "assumptions": []}
        (self.root / "docs" / "state.json").write_text(json.dumps(state), encoding="utf-8")
        env = dict(os.environ, IAM_ROOT=str(self.root))
        p = subprocess.run([sys.executable, str(REPO / "tools" / "memory.py"), "brief"],
                           capture_output=True, text=True, env=env)
        return p.stdout

    def test_brief_without_registry(self):
        out = self.brief()
        self.assertIn("ТЕКУЩИЙ СРЕЗ: T1", out)
        self.assertNotIn("Реестр требований", out)

    def test_brief_with_registry(self):
        self.write([mk(), mk(id="R-002", status="implemented")])
        out = self.brief()
        self.assertIn("Реестр требований: 2 требований: verified 0, implemented 1, planned 1, waived 0", out)

    def test_brief_survives_broken_registry(self):
        (self.root / "requirements.toml").write_text("[[req\n", encoding="utf-8")
        out = self.brief()
        self.assertIn("НЕ ЧИТАЕТСЯ", out)
        self.assertIn("ТЕКУЩИЙ СРЕЗ: T1", out)


class RealRegistryTest(unittest.TestCase):
    """Страж реального репозитория: реестр проходит проверку, план полностью покрыт, документ покрытия свеж."""

    def run_real(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "IAM_ROOT"}
        p = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env, cwd=REPO)
        return p.returncode, p.stdout + p.stderr

    def test_real_validate(self):
        code, out = self.run_real("validate")
        self.assertEqual(code, 0, out)

    def test_real_coverage_doc_fresh(self):
        code, out = self.run_real("coverage", "--check", "docs/spec/plan-coverage.md")
        self.assertEqual(code, 0, out)

    def test_real_scale(self):
        import tomllib
        data = tomllib.loads((REPO / "requirements.toml").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(data["req"]), 120)

    def test_all_19_required_scenarios_registered(self):
        import tomllib
        data = tomllib.loads((REPO / "requirements.toml").read_text(encoding="utf-8"))
        scen = [r for r in data["req"] if r["text"].startswith("Обязательный сценарий.")]
        self.assertEqual(len(scen), 19)


if __name__ == "__main__":
    unittest.main()
