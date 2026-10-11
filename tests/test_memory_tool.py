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


def make_root(tmp: Path, filled: bool = True, gates=None) -> None:
    (tmp / "docs" / "slices").mkdir(parents=True)
    state = {
        "project": "t", "current_slice": "T1", "revision": 1,
        "slices": {
            "T1": {"title": "a", "stage": 1, "depends": [], "status": "todo"},
            "T2": {"title": "b", "stage": 1, "depends": ["T1"], "status": "todo"},
            "T3": {"title": "c", "stage": 1, "depends": ["T2"], "status": "todo"},
        },
        "gates": gates or [], "assumptions": [],
    }
    (tmp / "docs" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    tail = "сделано\n" if filled else "ИТОГ-НЕ-ЗАПОЛНЕН\n"
    for sid in ("T1", "T2", "T3"):
        (tmp / "docs" / "slices" / f"{sid}-x.md").write_text(HEADINGS + tail, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_tool(self, *args, ok=True, err=None):
        r = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                           env={**os.environ, "IAM_ROOT": str(self.tmp)})
        out = r.stdout + r.stderr
        if ok:
            self.assertEqual(r.returncode, 0, out)
        else:
            self.assertNotEqual(r.returncode, 0, out)
            self.assertNotIn("Traceback", out)  # отказ должен быть управляемым, а не падением
            if err:
                self.assertIn(err, out)
        return r

    def state(self):
        return json.loads((self.tmp / "docs" / "state.json").read_text(encoding="utf-8"))

    def write_state(self, st):
        (self.tmp / "docs" / "state.json").write_text(json.dumps(st), encoding="utf-8")


class LifecycleTest(Base):
    def test_full_cycle(self):
        make_root(self.tmp)
        self.run_tool("render")
        self.run_tool("check")
        self.run_tool("start", "T1")
        st = self.state()
        self.assertEqual(st["slices"]["T1"]["status"], "in_progress")
        self.assertTrue(st["slices"]["T1"]["started"])
        self.run_tool("finish", "T1", "-m", "готово")
        st = self.state()
        self.assertEqual(st["slices"]["T1"]["status"], "done")
        self.assertEqual(st["current_slice"], "T2")
        self.assertGreater(st["revision"], 1)
        self.assertIn("T1 — закрыт", (self.tmp / "docs" / "JOURNAL.md").read_text(encoding="utf-8"))
        self.run_tool("check")

    def test_dependency_enforced_on_start(self):
        make_root(self.tmp)
        self.run_tool("start", "T2", ok=False, err="не закрыты")

    def test_dependency_enforced_on_finish(self):
        make_root(self.tmp)
        self.run_tool("start", "T2", "--force")
        self.run_tool("finish", "T2", "-m", "x", ok=False, err="зависимости")
        self.assertEqual(self.state()["slices"]["T2"]["status"], "in_progress")

    def test_second_in_progress_refused(self):
        make_root(self.tmp)
        self.run_tool("start", "T1")
        self.run_tool("start", "T2", "--force", ok=False, err="уже в работе")

    def test_finish_requires_summary_filled(self):
        make_root(self.tmp, filled=False)
        self.run_tool("start", "T1")
        self.run_tool("finish", "T1", "-m", "x", ok=False, err="Итог")
        self.assertEqual(self.state()["slices"]["T1"]["status"], "in_progress")

    def test_finish_rejects_empty_summary_without_marker(self):
        make_root(self.tmp)
        (self.tmp / "docs" / "slices" / "T1-x.md").write_text(HEADINGS, encoding="utf-8")  # пустой «Итог»
        self.run_tool("start", "T1")
        self.run_tool("finish", "T1", "-m", "x", ok=False, err="Итог")

    def test_finish_requires_in_progress(self):
        make_root(self.tmp)
        self.run_tool("finish", "T1", "-m", "x", ok=False, err="не в работе")

    def test_start_done_refused(self):
        make_root(self.tmp)
        self.run_tool("start", "T1")
        self.run_tool("finish", "T1", "-m", "x")
        self.run_tool("start", "T1", ok=False, err="уже закрыт")


class GateTest(Base):
    def test_gate_add_resolve_and_assume(self):
        make_root(self.tmp)
        self.run_tool("gate-add", "T1", "-m", "нужен токен")
        self.assertEqual(self.state()["gates"][0]["id"], "G01")
        self.run_tool("gate-resolve", "G01", "-m", "дано")
        self.assertEqual(self.state()["gates"][0]["status"], "resolved")
        self.run_tool("gate-resolve", "G01", "-m", "ещё раз", ok=False, err="уже закрыт")
        self.assertEqual(self.state()["gates"][0]["answer"], "дано")
        self.run_tool("assume", "A9", "-m", "допущение", "--status", "active")
        self.run_tool("assume", "A9", "-m", "уточнено", "--status", "confirmed")
        a = self.state()["assumptions"]
        self.assertEqual((len(a), a[0]["text"], a[0]["status"]), (1, "уточнено", "confirmed"))
        self.run_tool("assume", "A9", "-m", "x", "--status", "мусор", ok=False, err="статус допущения")
        self.run_tool("check")

    def test_gate_blocks_finish(self):
        make_root(self.tmp)
        self.run_tool("gate-add", "T1", "-m", "решение", "--blocks-finish", "T1")
        self.run_tool("start", "T1")
        self.run_tool("finish", "T1", "-m", "x", ok=False, err="G01")
        self.run_tool("gate-resolve", "G01", "-m", "ок")
        self.run_tool("finish", "T1", "-m", "x")

    def test_gate_blocks_start(self):
        make_root(self.tmp)
        self.run_tool("gate-add", "T1", "-m", "проверка на Mac", "--blocks-start", "T2")
        self.run_tool("start", "T1")
        self.run_tool("finish", "T1", "-m", "x")
        self.run_tool("start", "T2", ok=False, err="G01")
        self.run_tool("gate-resolve", "G01", "-m", "ок")
        self.run_tool("start", "T2")

    def test_gate_unknown_slice_refused(self):
        make_root(self.tmp)
        self.run_tool("gate-add", "T1", "-m", "x", "--blocks-start", "ZZ", ok=False, err="нет среза")

    def test_brief_shows_open_gates_of_done_slice_as_debt(self):
        make_root(self.tmp)
        self.run_tool("gate-add", "T1", "-m", "живая проверка")
        self.run_tool("start", "T1")
        self.run_tool("finish", "T1", "-m", "на заглушке")
        out = self.run_tool("brief").stdout
        self.assertIn("G01", out)
        self.assertIn("ДОЛГ", out)


class BlockTest(Base):
    def test_block_and_unblock(self):
        make_root(self.tmp)
        self.run_tool("start", "T1")
        self.run_tool("block", "T1", "-m", "жду пользователя")
        st = self.state()
        self.assertEqual(st["slices"]["T1"]["status"], "blocked")
        self.assertIsNone(st["current_slice"])
        out = self.run_tool("brief").stdout
        self.assertIn("заблокирована", out)
        self.assertIn("жду пользователя", out)
        self.assertNotIn("Все срезы закрыты", out)
        self.run_tool("check")
        self.run_tool("unblock", "T1")
        st = self.state()
        self.assertEqual(st["current_slice"], "T1")
        self.assertNotIn("blocked_reason", st["slices"]["T1"])

    def test_unblock_requires_blocked(self):
        make_root(self.tmp)
        self.run_tool("unblock", "T1", ok=False, err="не заблокирован")

    def test_block_done_refused(self):
        make_root(self.tmp)
        self.run_tool("start", "T1")
        self.run_tool("finish", "T1", "-m", "x")
        self.run_tool("block", "T1", "-m", "x", ok=False, err="нельзя заблокировать")


class CheckTest(Base):
    def test_missing_headings(self):
        make_root(self.tmp)
        self.run_tool("render")
        (self.tmp / "docs" / "slices" / "T2-x.md").write_text("# нет разделов\n", encoding="utf-8")
        self.run_tool("check", ok=False, err="нет раздела")

    def test_prefix_heading_does_not_count(self):
        make_root(self.tmp)
        (self.tmp / "docs" / "slices" / "T2-x.md").write_text(
            HEADINGS.replace("## Итог", "## Итоговая заметка"), encoding="utf-8")
        self.run_tool("render")
        self.run_tool("check", ok=False, err="Итог")

    def test_stale_state_md(self):
        make_root(self.tmp)
        self.run_tool("render")
        (self.tmp / "docs" / "STATE.md").write_text("вручную испорчено", encoding="utf-8")
        self.run_tool("check", ok=False, err="STATE.md")

    def test_corrupt_state_json_is_controlled_error(self):
        make_root(self.tmp)
        st = self.state()
        del st["slices"]["T1"]["depends"]
        self.write_state(st)
        self.run_tool("check", ok=False, err="повреждён")

    def test_done_with_unmet_dependency(self):
        make_root(self.tmp)
        st = self.state()
        st["slices"]["T2"].update(status="done", finished="2026-01-01T00:00Z")
        self.write_state(st)
        self.run_tool("render")
        self.run_tool("check", ok=False, err="зависимость T1 не закрыта")

    def test_bad_current_slice(self):
        make_root(self.tmp)
        st = self.state()
        st["current_slice"] = "T3"
        self.write_state(st)
        self.run_tool("render")
        self.run_tool("check", ok=False, err="current_slice")

    def test_gate_inconsistency(self):
        make_root(self.tmp, gates=[
            {"id": "G01", "slice": "T1", "status": "open", "blocks_finish": ["T1"]},
            {"id": "G01", "slice": "ZZ", "status": "weird"}])
        self.run_tool("render")
        r = self.run_tool("check", ok=False)
        self.assertIn("повторяющиеся ID барьеров", r.stdout)
        self.assertIn("не существует", r.stdout)

    def test_unknown_slice_arg(self):
        make_root(self.tmp)
        self.run_tool("start", "ZZ", ok=False, err="нет среза")


class JournalTest(Base):
    def test_multiline_message_does_not_break_brief(self):
        make_root(self.tmp)
        self.run_tool("log", "-m", "строка\n## фальшивый заголовок\nхвост")
        out = self.run_tool("brief").stdout
        self.assertNotIn("фальшивый заголовок", out)

    def test_checkpoint_commits_and_reports(self):
        make_root(self.tmp)
        subprocess.run(["git", "init", "-q", "-b", "wip"], cwd=self.tmp, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A"], cwd=self.tmp, check=True)
        env = {**os.environ, "IAM_PUSH_ATTEMPTS": "1", "IAM_ROOT": str(self.tmp), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "a@b",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "a@b"}
        r = subprocess.run([sys.executable, str(TOOL), "checkpoint", "-m", "точка"],
                           capture_output=True, text=True, env=env)
        # remote нет: push обязан упасть управляемо, но коммит уже должен существовать
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        log = subprocess.run(["git", "log", "--oneline"], cwd=self.tmp, capture_output=True, text=True).stdout
        self.assertIn("WIP: точка", log)


class SyncTest(Base):
    def test_sync_finds_fresher_branch(self):
        make_root(self.tmp)
        git = lambda *a, cwd=self.tmp: subprocess.run(
            ["git", "-c", "user.email=a@b", "-c", "user.name=t", *a], cwd=cwd, check=True, capture_output=True)
        remote = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, remote, True)
        git("init", "-q", "-b", "claude/old")
        git("add", "-A"); git("commit", "-q", "-m", "c1")
        git("remote", "add", "origin", str(remote))
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        git("push", "-q", "origin", "claude/old")
        st = self.state(); st["revision"] = 9; self.write_state(st)
        git("commit", "-qam", "c2")
        git("push", "-q", "origin", "HEAD:refs/heads/claude/new")
        git("reset", "-q", "--hard", "HEAD~1")  # локально ревизия 1
        out = self.run_tool("sync").stdout
        self.assertIn("УСТАРЕЛА", out)
        self.assertIn("origin/claude/new", out)


class RealRepoTest(unittest.TestCase):
    """Страж: реальная память репозитория согласована (render/slices/state)."""

    def test_real_memory_is_consistent(self):
        r = subprocess.run([sys.executable, str(TOOL), "check"], capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if k != "IAM_ROOT"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
