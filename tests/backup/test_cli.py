"""Командная строка `iam backup|export|restore|verify` (S06): коды выхода и сообщения."""
import contextlib
import io
import os
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from iam import cli
from iam.backup import list_backups
from iam.storage import Store

from .helpers import BackupCase, PID

SRC = Path(__file__).resolve().parents[2] / "src"
CANARY = "CLI-КАНАРЕЙКА-" + "5d8e1f77"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTest(BackupCase):
    def setUp(self):
        super().setUp()
        self.persona().store.close()      # личность без работающего процесса: база свободна
        self.bdir = str(self.backups)

    def test_backup_offline_records_event_and_rotates(self):
        for _ in range(4):
            code, out, err = run("backup", "--db", str(self.db), "--backup-dir", self.bdir, "--reason", "проба")
            self.assertEqual(code, 0, err)
        self.assertIn("снимок snp_", out)
        self.assertIn("удалена старая копия", out)
        self.assertEqual(len(list_backups(self.backups)[0]), 3)
        with Store.open(self.db, readonly=True) as s:
            self.assertEqual(sum(e.type == "system.backup_created" for e in s.events()), 4)

    def test_backup_while_process_owns_the_database(self):
        owner = Store.open(self.db)
        self.addCleanup(owner.close)
        code, out, err = run("backup", "--db", str(self.db), "--backup-dir", self.bdir)
        self.assertEqual(code, 0, err)
        self.assertIn("база занята работающим процессом", out)
        self.assertEqual(len(list_backups(self.backups)[0]), 1)

    def test_backup_of_missing_database_fails_cleanly(self):
        code, _, err = run("backup", "--db", str(self.tmp / "нет.db"), "--backup-dir", self.bdir)
        self.assertEqual(code, 1)
        self.assertIn("ОШИБКА", err)

    def test_backup_reports_corrupt_neighbour(self):
        run("backup", "--db", str(self.db), "--backup-dir", self.bdir)
        victim = list_backups(self.backups)[0][0].path
        raw = bytearray(victim.read_bytes()); raw[len(raw) // 2] ^= 0xFF; victim.write_bytes(raw)
        code, _, err = run("backup", "--db", str(self.db), "--backup-dir", self.bdir)
        self.assertEqual(code, 1)
        self.assertIn("повреждённая копия", err)

    def test_default_backup_dir_is_used(self):
        personas = self.tmp / "root" / "personas"
        personas.mkdir(parents=True)
        db = personas / f"{PID}.db"
        db.write_bytes(self.db.read_bytes())
        code, _, err = run("backup", "--db", str(db))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(list_backups(self.tmp / "root" / "backups" / PID)[0]), 1)

    def test_backup_with_unopenable_database_still_reports_error(self):
        bad = self.tmp / "bad.db"
        bad.write_bytes(b"not a database " * 100)
        code, _, err = run("backup", "--db", str(bad), "--backup-dir", self.bdir)
        self.assertEqual(code, 1)

    def test_export_and_secret_env(self):
        out_zip = self.tmp / "e.zip"
        with mock.patch.dict(os.environ, {"MY_CANARY": CANARY}):
            code, out, err = run("export", "--db", str(self.db), "--out", str(out_zip), "--secret-env", "MY_CANARY")
        self.assertEqual(code, 0, err)
        self.assertIn("экспорт:", out)
        with zipfile.ZipFile(out_zip) as z:
            self.assertNotIn(CANARY, b"".join(z.read(n) for n in z.namelist()).decode())
        code, _, err = run("export", "--db", str(self.db), "--out", str(self.tmp / "e2.zip"), "--secret-env", "NO_SUCH_VAR_X")
        self.assertEqual(code, 2)
        self.assertIn("не задана", err)
        code, _, err = run("export", "--db", str(self.db), "--out", str(out_zip))
        self.assertEqual(code, 1)      # файл уже есть

    def test_export_reports_redactions(self):
        s = Store.open(self.db)
        from iam.state import EventSpec, StateController
        StateController(s, clock=self.clock(), recover=False).record_event(
            EventSpec("input.arrived", "rx:tg:5", {"text": "токен " + "123456789" + ":" + "AAH" + "x" * 32}))
        s.close()
        code, out, _ = run("export", "--db", str(self.db), "--out", str(self.tmp / "r.zip"))
        self.assertEqual(code, 0)
        self.assertIn("удалено секретов", out)

    def test_restore_then_verify_everything(self):
        run("backup", "--db", str(self.db), "--backup-dir", self.bdir)
        bk = list_backups(self.backups)[0][0].path
        code, out, err = run("restore", str(bk), "--to", str(self.tmp / "r"), "--reason", "проба", "--live-db", str(self.db))
        self.assertEqual(code, 0, err)
        self.assertIn("эпоха 1 → 2", out)
        self.assertIn("живая база не тронута", out)
        self.assertIn("отброшены события", out)      # после копии в живой базе записано system.backup_created
        code, _, err = run("restore", str(bk), "--to", str(self.tmp / "r"), "--reason", "ещё раз")
        self.assertEqual((code, "уже существует" in err), (1, True))
        self.assertEqual(run("verify", str(bk))[0], 0)
        self.assertEqual(run("verify", str(self.tmp / "r" / f"{PID}.db"))[0], 0)
        self.assertEqual(run("verify", self.bdir)[0], 0)
        run("export", "--db", str(self.db), "--out", str(self.tmp / "v.zip"))
        code, out, _ = run("verify", str(self.tmp / "v.zip"))
        self.assertEqual(code, 0)
        self.assertIn("ОК", out)

    def test_verify_failures_and_usage_errors(self):
        run("backup", "--db", str(self.db), "--backup-dir", self.bdir)
        bk = list_backups(self.backups)[0][0].path
        raw = bytearray(bk.read_bytes()); raw[len(raw) // 2] ^= 0xFF; bk.write_bytes(raw)
        code, out, _ = run("verify", str(bk))
        self.assertEqual(code, 1)
        self.assertIn("ОШИБКА", out)
        self.assertEqual(run("verify", self.bdir)[0], 1)
        empty = self.tmp / "пусто"; empty.mkdir()
        self.assertEqual(run("verify", str(empty))[0], 1)
        self.assertEqual(run("verify", str(self.tmp / "нет"))[0], 2)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main([])

    def test_verify_plain_database_prints_notes(self):
        code, out, _ = run("verify", str(self.db))
        self.assertEqual(code, 0, out)
        old = self.tmp / "old.db"
        old.write_bytes(self.db.read_bytes())
        import sqlite3
        con = sqlite3.connect(old); con.execute("DELETE FROM schema_version"); con.commit(); con.close()
        code, out, _ = run("verify", str(old))
        self.assertEqual(code, 0, out)
        self.assertIn("примечание", out)

    def test_os_errors_are_reported_not_traced(self):
        code, _, err = run("export", "--db", str(self.tmp / "нет.db"), "--out", str(self.tmp / "q.zip"))
        self.assertEqual((code, err.startswith("ОШИБКА")), (1, True))

    def test_restore_without_live_db_prints_no_gap(self):
        run("backup", "--db", str(self.db), "--backup-dir", self.bdir)
        bk = list_backups(self.backups)[0][0].path
        code, out, _ = run("restore", str(bk), "--to", str(self.tmp / "r2"), "--reason", "без живой базы")
        self.assertEqual(code, 0)
        self.assertNotIn("отброшены", out)

    def test_refusals_have_their_own_messages(self):
        from iam.backup import NotEnoughSpace, SecretFound
        with mock.patch.object(cli, "export_persona", side_effect=SecretFound(["events.jsonl: telegram_bot_token 12…(44 зн.)"])):
            code, _, err = run("export", "--db", str(self.db), "--out", str(self.tmp / "a.zip"))
        self.assertEqual((code, err.startswith("ОТКАЗ: в экспорте найдены секреты")), (1, True))
        self.assertNotIn("AAH", err)
        with mock.patch.object(cli, "export_persona", side_effect=NotEnoughSpace(1, 9)):
            code, _, err = run("export", "--db", str(self.db), "--out", str(self.tmp / "b.zip"))
        self.assertEqual((code, "ОТКАЗ: мало места" in err), (1, True))

    def test_runs_as_module_and_console_entry(self):
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        r = subprocess.run([sys.executable, "-m", "iam.cli", "verify", str(self.db)], env=env, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.startswith("ОК")), (0, True), r.stderr)
