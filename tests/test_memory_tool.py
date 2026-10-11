"""Тесты tools/memory.py на временной копии репозитория (IAM_ROOT)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "memory.py"

HEADINGS = "## Простыми словами\n.\n## Что делаем\n.\n## Границы файлов\n.\n## Проверки\n.\n## Итог\n"


def make_root(tmp: Path, filled: bool) -> None:
    (tmp / "docs" / "slices").mkdir(parents=True)
    state = {
        "project": "t", "current_slice": "T1",
        "slices": {
            "T1": {"title": "a", "stage": 1, "depends": [], "status": "todo"},
            "T2": {"title": "b", "stage": 1, "depends": ["T1"], "status": "todo"},
        },
        "gates": [], "assumptions": [],
    }
    (tmp / "docs" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    tail = "сделано\n" if filled else "ИТОГ-НЕ-ЗАПОЛНЕН\n"
    for sid in ("T1", "T2"):
        (tmp / "docs" / "slices" / f"{sid}-x.md").write_text(HEADINGS + tail, encoding="utf-8")


class MemoryToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_tool(self, *args, expect_ok=True):
        r = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                           env={**os.environ, "IAM_ROOT": str(self.tmp)})
        if expect_ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout)
        return r

    def state(self):
        return json.loads((self.tmp / "docs" / "state.json").read_text(encoding="utf-8"))

    def test_full_cycle(self):
        make_root(self.tmp, filled=True)
        self.run_tool("render")
        self.run_tool("check")
        self.run_tool("start", "T1")
        self.assertEqual(self.state()["slices"]["T1"]["status"], "in_progress")
        self.run_tool("finish", "T1", "-m", "готово")
        st = self.state()
        self.assertEqual(st["slices"]["T1"]["status"], "done")
        self.assertEqual(st["current_slice"], "T2")
        self.assertIn("T1 — закрыт", (self.tmp / "docs" / "JOURNAL.md").read_text(encoding="utf-8"))
        self.run_tool("check")

    def test_dependency_enforced(self):
        make_root(self.tmp, filled=True)
        self.run_tool("start", "T2", expect_ok=False)
        self.run_tool("start", "T2", "--force")

    def test_finish_requires_summary_filled(self):
        make_root(self.tmp, filled=False)
        self.run_tool("start", "T1")
        self.run_tool("finish", "T1", "-m", "x", expect_ok=False)
        self.assertEqual(self.state()["slices"]["T1"]["status"], "in_progress")

    def test_finish_requires_in_progress(self):
        make_root(self.tmp, filled=True)
        self.run_tool("finish", "T1", "-m", "x", expect_ok=False)

    def test_gates_and_assumptions(self):
        make_root(self.tmp, filled=True)
        self.run_tool("gate-add", "T1", "-m", "нужен токен")
        self.assertEqual(self.state()["gates"][0]["id"], "G01")
        self.run_tool("gate-resolve", "G01", "-m", "дано")
        self.assertEqual(self.state()["gates"][0]["status"], "resolved")
        self.run_tool("assume", "A9", "-m", "допущение", "--status", "active")
        self.assertEqual(self.state()["assumptions"][0]["id"], "A9")
        self.run_tool("check")

    def test_check_detects_problems(self):
        make_root(self.tmp, filled=True)
        self.run_tool("render")
        (self.tmp / "docs" / "slices" / "T2-x.md").write_text("# нет разделов\n", encoding="utf-8")
        r = self.run_tool("check", expect_ok=False)
        self.assertIn("нет раздела", r.stdout)

    def test_check_detects_stale_state_md(self):
        make_root(self.tmp, filled=True)
        self.run_tool("render")
        (self.tmp / "docs" / "STATE.md").write_text("вручную испорчено", encoding="utf-8")
        r = self.run_tool("check", expect_ok=False)
        self.assertIn("STATE.md", r.stdout)

    def test_block_moves_pointer(self):
        make_root(self.tmp, filled=True)
        self.run_tool("start", "T1")
        self.run_tool("block", "T1", "-m", "жду")
        st = self.state()
        self.assertEqual(st["slices"]["T1"]["status"], "blocked")
        self.assertIsNone(st["current_slice"])  # T2 зависит от незакрытого T1
        self.run_tool("unblock", "T1")
        self.assertEqual(self.state()["current_slice"], "T1")

    def test_brief_runs(self):
        make_root(self.tmp, filled=True)
        r = self.run_tool("brief")
        self.assertIn("ТЕКУЩИЙ СРЕЗ: T1", r.stdout)


class RealRepoTest(unittest.TestCase):
    def test_real_memory_is_consistent(self):
        r = subprocess.run([sys.executable, str(TOOL), "check"], capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if k != "IAM_ROOT"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
