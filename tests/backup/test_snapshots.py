"""Снимки: онлайн-копия, проверка, ротация до трёх, повреждённая копия, копия перед миграцией (R-200, R-201)."""
import datetime as dt
import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from iam.backup import (BackupCorrupt, BackupError, NotEnoughSpace, SpaceGuard, check_backup, create_backup,
                        default_backup_dir, list_backups, migration_hook, open_for_write, verify_database)
from iam.backup import snapshots
from iam.state import StateController, verify
from iam.storage import LATEST, MIGRATIONS, Migration, Store, connect, current_version, migrate

from .helpers import BackupCase, PID, T0


class SnapshotTest(BackupCase):
    def test_snapshot_is_verified_and_described_by_manifest(self):
        c = self.persona()
        res = create_backup(self.db, self.backups, now=T0, reason="проба")
        m = res.info.manifest
        self.assertTrue(res.info.path.is_file() and res.info.manifest_path.is_file())
        self.assertEqual((m["persona_id"], m["kind"], m["reason"], m["schema_version"], m["epoch"], m["state_version"]),
                         (PID, "regular", "проба", LATEST, 1, c.state_version))
        self.assertEqual(m["last_seq"], c.store.last_seq())
        self.assertEqual(json.loads(res.info.manifest_path.read_text(encoding="utf-8")), m)
        rep = check_backup(res.info.path)
        self.assertTrue(rep.ok, rep.problems)
        self.assertEqual(list(self.backups.glob(".*")), [Path(self.backups / ".lock")])   # недоделанного не осталось

    def test_snapshot_is_online_and_independent_of_the_writer(self):
        """Писатель держит замок и продолжает жить; копия — отдельный самодостаточный файл."""
        c = self.persona()
        res = create_backup(self.db, self.backups, now=T0)
        self.live_more(c)                                    # жизнь после копии
        self.assertEqual(verify(c.store), [])
        with Store.open(res.info.path, readonly=True) as copy:
            self.assertEqual(copy.last_seq(), res.info.manifest["last_seq"])
            self.assertLess(copy.last_seq(), c.store.last_seq())
            self.assertEqual(verify(copy), [])
        self.assertEqual(list(self.backups.glob("*-wal")) + list(self.backups.glob("*-shm")), [])

    def test_backup_event_is_recorded_by_the_controller_after_the_copy(self):
        c = self.persona()
        res = create_backup(self.db, self.backups, now=T0, controller=c)
        ev = c.store.get_event(c.store.last_seq())
        self.assertEqual((ev.type, ev.changes_state, ev.idempotency_key, ev.event_id),
                         ("system.backup_created", False, f"bak:{res.info.snapshot_id}", res.event_id))
        self.assertEqual(ev.payload["sha256"], res.info.manifest["sha256"])
        self.assertLess(res.info.manifest["last_seq"], ev.seq, "событие о копии в самой копии быть не может")
        again = create_backup(self.db, self.backups, now=self.tick())
        self.assertNotEqual(again.info.snapshot_id, res.info.snapshot_id)

    def test_rotation_keeps_exactly_three_latest(self):
        """R-201: после четвёртого снимка остаются три последних."""
        self.persona()
        made = [create_backup(self.db, self.backups, now=self.tick()) for _ in range(4)]
        self.assertEqual(made[3].rotated, [made[0].info.path])
        infos, broken = list_backups(self.backups)
        self.assertEqual([i.snapshot_id for i in infos], [m.info.snapshot_id for m in made[1:]])
        self.assertEqual(broken, [])
        self.assertEqual(len(list(self.backups.glob("*.db"))), 3)
        self.assertEqual(len(list(self.backups.glob("*.json"))), 3)
        self.assertFalse(made[0].info.manifest_path.exists())
        for _ in range(3):
            create_backup(self.db, self.backups, now=self.tick())
        self.assertEqual(len(list(self.backups.glob("*.db"))), 3)

    def test_fresh_snapshot_survives_rotation_even_if_the_clock_went_back(self):
        self.persona()
        for _ in range(3):
            create_backup(self.db, self.backups, now=self.tick())
        back = create_backup(self.db, self.backups, now=T0 - dt.timedelta(days=3))
        self.assertTrue(back.info.path.exists() and check_backup(back.info.path).ok)
        self.assertNotIn(back.info.path, back.rotated)
        self.assertEqual(len(list_backups(self.backups)[0]), 3)

    def test_paths_with_uri_special_characters(self):
        self.persona().store.close()
        for name in ("a#b", "a?b", "a%20b", "a b"):
            with self.subTest(name=name):
                d = self.tmp / name
                d.mkdir()
                shutil.copy(self.db, d / self.db.name)
                res = create_backup(d / self.db.name, d / "b", now=self.tick())
                self.assertTrue(check_backup(res.info.path).ok)
                self.assertTrue(verify_database(res.info.path).ok)

    def test_keep_parameter_and_bad_arguments(self):
        self.persona()
        for _ in range(3):
            create_backup(self.db, self.backups, now=self.tick(), keep=2)
        self.assertEqual(len(list_backups(self.backups)[0]), 2)
        with self.assertRaises(ValueError):
            create_backup(self.db, self.backups, keep=0)
        with self.assertRaises(ValueError):
            create_backup(self.db, self.backups, kind="weird")
        with self.assertRaises(BackupError):
            create_backup(self.tmp / "нет.db", self.backups)

    def test_corrupt_snapshot_is_detected_and_not_counted(self):
        self.persona()
        made = [create_backup(self.db, self.backups, now=self.tick()) for _ in range(3)]
        victim = made[1].info.path
        raw = bytearray(victim.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        victim.write_bytes(raw)
        rep = check_backup(victim)
        self.assertFalse(rep.ok)
        self.assertTrue(any("sha256" in p for p in rep.problems), rep.problems)
        newest = create_backup(self.db, self.backups, now=self.tick())
        self.assertEqual(newest.corrupt, [victim], "повреждённая копия не удалена молча, а названа")
        self.assertTrue(victim.exists())
        healthy = [i.snapshot_id for i in list_backups(self.backups)[0] if check_backup(i.path, deep=False).ok]
        self.assertEqual(healthy, [made[0].info.snapshot_id, made[2].info.snapshot_id, newest.info.snapshot_id])

    def test_other_damages_are_detected(self):
        self.persona()
        a = create_backup(self.db, self.backups, now=self.tick())
        path, man = a.info.path, a.info.manifest_path
        good_man = man.read_text(encoding="utf-8")
        man.write_text("{не json", encoding="utf-8")
        self.assertIn("манифест", " ".join(check_backup(path).problems))
        man.write_text(json.dumps({"format": 1}), encoding="utf-8")
        self.assertIn("нет полей", " ".join(check_backup(path).problems))
        man.write_text(json.dumps({**json.loads(good_man), "format": 9}), encoding="utf-8")
        self.assertIn("формат манифеста", " ".join(check_backup(path).problems))
        man.unlink()
        self.assertEqual(list_backups(self.backups), ([], [path]))
        man.write_text(good_man, encoding="utf-8")
        self.assertTrue(check_backup(path).ok)
        path.write_bytes(path.read_bytes()[:-100])     # усечён
        self.assertIn("размер", " ".join(check_backup(path).problems))
        path.unlink()
        self.assertIn("файла копии", " ".join(check_backup(path).problems))
        mismatch = json.loads(good_man)
        mismatch["last_seq"] += 5
        a2 = create_backup(self.db, self.backups, now=self.tick())
        bad = json.loads(a2.info.manifest_path.read_text(encoding="utf-8"))
        bad["last_seq"] += 5
        a2.info.manifest_path.write_text(json.dumps(bad), encoding="utf-8")
        self.assertIn("last_seq", " ".join(check_backup(a2.info.path).problems))

    def test_failed_snapshot_leaves_directory_untouched(self):
        self.persona()
        good = create_backup(self.db, self.backups, now=self.tick())
        before = sorted(p.name for p in self.backups.iterdir())
        with mock.patch.object(snapshots, "verify_database", return_value=snapshots.VerifyReport(["сломано"])):
            with self.assertRaises(BackupCorrupt):
                create_backup(self.db, self.backups, now=self.tick())
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), before)
        self.assertTrue(check_backup(good.info.path).ok)

    def test_disk_full_during_copy_is_passed_through_and_cleaned(self):
        self.persona()
        good = create_backup(self.db, self.backups, now=self.tick())
        before = sorted(p.name for p in self.backups.iterdir())
        with mock.patch.object(snapshots, "ro_connect", side_effect=sqlite3.OperationalError("database or disk is full")):
            with self.assertRaises(sqlite3.OperationalError):
                create_backup(self.db, self.backups, now=self.tick())
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), before)
        self.assertTrue(check_backup(good.info.path).ok)

    def test_failure_after_naming_removes_the_half_made_snapshot(self):
        self.persona()
        good = create_backup(self.db, self.backups, now=self.tick())
        before = sorted(p.name for p in self.backups.iterdir())
        with mock.patch.object(snapshots, "_write_json_atomic", side_effect=OSError("сбой записи манифеста")):
            with self.assertRaises(OSError):
                create_backup(self.db, self.backups, now=self.tick())
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), before)
        self.assertTrue(check_backup(good.info.path).ok)

    def test_no_space_means_no_snapshot_and_no_changes(self):
        self.persona()
        create_backup(self.db, self.backups, now=self.tick())
        before = sorted(p.name for p in self.backups.iterdir())
        full = SpaceGuard(self.backups, usage=lambda p: (100, 100, 10))
        with self.assertRaises(NotEnoughSpace):
            create_backup(self.db, self.backups, now=self.tick(), space=full)
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), before)

    def test_stale_partials_are_swept(self):
        self.persona()
        self.backups.mkdir()
        stale = self.backups / ".snp_OLD.partial"
        stale.write_bytes("мусор".encode())
        create_backup(self.db, self.backups, now=self.tick())
        self.assertFalse(stale.exists())

    def test_default_backup_dir(self):
        self.assertEqual(default_backup_dir(self.tmp / "personas" / "per_X.db"), (self.tmp / "backups" / "per_X").resolve())
        self.assertEqual(default_backup_dir(self.tmp / "live" / "per_X.db"), (self.tmp / "live" / "backups" / "per_X").resolve())

    def test_snapshot_of_empty_database_is_refused(self):
        empty = self.tmp / "empty.db"
        sqlite3.connect(empty).close()
        with self.assertRaises(BackupCorrupt):
            create_backup(empty, self.backups)
        self.assertEqual(list(self.backups.glob("*.db")), [])
        garbage = self.tmp / "garbage.db"
        garbage.write_bytes(b"not a sqlite file " * 100)
        with self.assertRaises(BackupCorrupt):
            create_backup(garbage, self.backups)


class VerifyDatabaseTest(BackupCase):
    def test_healthy_database_passes_even_with_a_live_writer(self):
        self.persona()
        rep = verify_database(self.db)
        self.assertTrue(rep.ok, rep.problems)
        self.assertTrue(verify_database(self.db, deep=False).ok)

    def test_missing_file_and_garbage(self):
        self.assertIn("файла нет", verify_database(self.tmp / "нет.db").problems[0])
        bad = self.tmp / "bad.db"
        bad.write_bytes(b"\x00" * 4096)
        self.assertFalse(verify_database(bad).ok)
        bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 5000)
        self.assertFalse(verify_database(bad).ok)

    def test_page_level_corruption_is_found(self):
        self.persona()
        copy = create_backup(self.db, self.backups, now=T0).info.path
        raw = bytearray(copy.read_bytes())
        for i in range(8192, min(len(raw), 8192 + 2048)):
            raw[i] = 0xAB
        copy.write_bytes(raw)
        self.assertFalse(verify_database(copy).ok)

    def test_tampered_journal_is_found_by_replay(self):
        """База цела для SQLite, но снимок не совпадает с журналом (правка в обход ядра)."""
        c = self.persona()
        c.store.close()
        con = sqlite3.connect(self.db)
        con.execute("DROP TRIGGER state_snapshots_no_update")
        con.execute("UPDATE state_snapshots SET state = replace(state, '0.6', '0.1') WHERE state_version = 3")
        con.commit()
        con.close()
        rep = verify_database(self.db)
        self.assertFalse(rep.ok)
        self.assertTrue(any("не совпадает" in p for p in rep.problems), rep.problems)

    def test_database_without_persona_is_reported(self):
        self.persona()
        copy = create_backup(self.db, self.backups, now=T0).info.path
        con = sqlite3.connect(copy)
        con.execute("DROP TRIGGER meta_fixed_delete")
        con.execute("DELETE FROM meta WHERE key = 'persona_id'")
        con.commit()
        con.close()
        self.assertIn("persona_id", " ".join(verify_database(copy).problems))

    def test_integrity_check_findings_are_reported(self):
        self.persona()
        real = snapshots.ro_connect

        class Fake:
            def execute(self, sql):
                return [("row 7 missing from index events_x",)]
            def close(self):
                pass
        with mock.patch.object(snapshots, "ro_connect", return_value=Fake()):
            rep = verify_database(self.db)
        self.assertEqual(rep.problems, ["integrity_check: row 7 missing from index events_x"])
        self.assertTrue(real and verify_database(self.db).ok)

    def test_older_format_is_a_note_not_an_error(self):
        self.persona()
        copy = create_backup(self.db, self.backups, now=T0).info.path
        con = sqlite3.connect(copy)
        con.execute("DELETE FROM schema_version")
        con.commit()
        con.close()
        rep = verify_database(copy)
        self.assertEqual(rep.problems, [])
        self.assertIn("старее", rep.notes[0])

    def test_store_that_cannot_be_opened_is_reported(self):
        self.persona()
        with mock.patch.object(snapshots.Store, "open", side_effect=snapshots.StorageError("замок")):
            self.assertIn("не открылась", " ".join(verify_database(self.db).problems))

    def test_newer_format_is_reported(self):
        self.persona()
        copy = create_backup(self.db, self.backups, now=T0).info.path
        con = sqlite3.connect(copy)
        con.execute("INSERT INTO schema_version VALUES (?, 'будущее', 'x')", (LATEST + 1,))
        con.commit()
        con.close()
        self.assertIn("новее", " ".join(verify_database(copy).problems))


V2 = Migration(LATEST + 1, "проба", ("CREATE TABLE probe (x INTEGER) STRICT",))


class MigrationBackupTest(BackupCase):
    """R-200: перед изменением формата базы обязательна свежая проверенная копия."""

    def make_v_old(self):
        c = self.persona()
        c.store.close()

    def test_copy_is_made_before_migration_and_describes_it(self):
        self.make_v_old()
        con = connect(self.db)
        hook = migration_hook(self.db, self.backups, now=T0)
        self.assertEqual(migrate(con, migrations=MIGRATIONS + (V2,), before=hook), [V2.version])
        self.assertEqual(current_version(con), V2.version)
        con.close()
        infos, _ = list_backups(self.backups / "pre-migration")
        self.assertEqual(len(infos), 1)
        m = infos[0].manifest
        self.assertEqual((m["kind"], m["schema_version"], m["migration"]), ("pre_migration", LATEST, {"from": LATEST, "to": V2.version}))
        self.assertTrue(check_backup(infos[0].path).ok)
        self.assertEqual(list(self.backups.glob("*.db")), [], "регулярные копии отдельно от копий перед миграцией")

    def test_migration_without_a_fresh_copy_is_rejected(self):
        self.make_v_old()
        con = connect(self.db)
        hook = migration_hook(self.db, self.backups, space=SpaceGuard(self.backups, usage=lambda p: (1, 1, 1)))
        with self.assertRaises(NotEnoughSpace):
            migrate(con, migrations=MIGRATIONS + (V2,), before=hook)
        self.assertEqual(current_version(con), LATEST, "миграция не началась")
        self.assertEqual(con.execute("SELECT name FROM sqlite_master WHERE name = 'probe'").fetchall(), [])
        con.close()
        self.assertEqual(list((self.backups / "pre-migration").glob("*.db")) if (self.backups / "pre-migration").exists() else [], [])

    def test_copy_failure_of_any_kind_rejects_migration(self):
        self.make_v_old()
        con = connect(self.db)
        hook = migration_hook(self.db, self.backups)
        with mock.patch.object(snapshots, "verify_database", return_value=snapshots.VerifyReport(["сломано"])):
            with self.assertRaises(BackupCorrupt):
                migrate(con, migrations=MIGRATIONS + (V2,), before=hook)
        self.assertEqual(current_version(con), LATEST)
        con.close()

    def test_pre_migration_rotation_is_separate_and_keeps_three(self):
        self.make_v_old()
        for _ in range(4):
            migration_hook(self.db, self.backups, now=self.tick())(LATEST, LATEST + 1)
        self.assertEqual(len(list((self.backups / "pre-migration").glob("*.db"))), 3)

    def test_open_for_write_copies_only_when_migration_is_pending(self):
        self.make_v_old()
        with open_for_write(self.db, self.backups) as store:
            self.assertEqual(store.persona_id, PID)
        self.assertFalse((self.backups / "pre-migration").exists(), "формат актуален: копии перед миграцией нет")
        with mock.patch("iam.storage.store.migrate") as m:
            with open_for_write(self.db, self.backups):
                pass
            self.assertTrue(callable(m.call_args.kwargs["before"]))
        with open_for_write(self.db) as store:     # каталог копий по умолчанию
            self.assertGreater(store.last_seq(), 0)
