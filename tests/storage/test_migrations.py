"""Мигратор (S04): v0→v1 на пустой и на заполненной базе, повторный запуск безопасен,
прерванная миграция не оставляет полсхемы, чужую базу и слишком новый формат не трогаем."""
import sqlite3
import unittest

from iam.storage import (LATEST, MIGRATIONS, Migration, SchemaOutdated, SchemaTooNew, Store, UnknownDatabase,
                         connect, current_version, migrate)

from .helpers import PID, TmpDirCase, ev, mem

V2 = Migration(2, "проба: заметки", ("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL) STRICT",
                                     "INSERT INTO notes (text) SELECT 'событие ' || seq FROM events"))
BROKEN = Migration(2, "сломанная", ("CREATE TABLE half (x INTEGER)", "CREATE TABLE half (x INTEGER)"))

EXPECTED_TABLES = {"schema_version", "meta", "events", "state_snapshots", "memory_records", "memory_fts", "inbox",
                   "outbox", "clock"}


def tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")
            if not r[0].startswith("sqlite_") and not r[0].startswith("memory_fts_")}


class MigrateTest(TmpDirCase):
    def con(self):
        c = connect(self.db)
        self.addCleanup(c.close)
        return c

    def test_empty_to_v1_and_rerun_is_noop(self):
        c = self.con()
        self.assertEqual(current_version(c), 0)
        self.assertEqual(migrate(c), [1])
        self.assertEqual(current_version(c), LATEST)
        self.assertEqual(tables(c), EXPECTED_TABLES)
        self.assertEqual(migrate(c), [])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchone()[0], "archive")

    def test_filled_base_survives_rerun_and_next_migration(self):
        s = self.new_store()
        for i in range(1, 6):
            s.append_event(ev(i))
        s.add_memory(mem("Кошка спит на окне"))
        s.close()
        with Store.open(self.db) as s2:  # повторный запуск миграций на заполненной базе
            self.assertEqual(s2.last_seq(), 5)
        c = self.con()
        calls = []
        self.assertEqual(migrate(c, migrations=MIGRATIONS + (V2,), before=lambda a, b: calls.append((a, b))), [2])
        self.assertEqual(calls, [(1, 2)], "перед миграцией непустой базы вызывается хук (копия — S06)")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 5)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM events").fetchone()[0], 5)
        self.assertEqual(migrate(c, migrations=MIGRATIONS + (V2,), before=lambda a, b: calls.append((a, b))), [])
        self.assertEqual(calls, [(1, 2)], "без ожидающих миграций хук не вызывается")

    def test_hook_not_called_for_fresh_base(self):
        calls = []
        migrate(self.con(), before=lambda a, b: calls.append((a, b)))
        self.assertEqual(calls, [])

    def test_target_limits_migrations(self):
        c = self.con()
        self.assertEqual(migrate(c, target=0), [])
        self.assertEqual(current_version(c), 0)

    def test_failed_migration_rolls_back_whole_step(self):
        c = self.con()
        migrate(c)
        with self.assertRaises(sqlite3.OperationalError):
            migrate(c, migrations=MIGRATIONS + (BROKEN,))
        self.assertEqual(current_version(c), 1)
        self.assertNotIn("half", tables(c))
        self.assertFalse(c.guard.migrating, "после сбоя сторож снова запрещает DDL")

    def test_concurrent_migrator_does_not_apply_twice(self):
        c = self.con()
        migrate(c)

        def other_process_migrates_first(a, b):
            other = connect(self.db)
            migrate(other, migrations=MIGRATIONS + (V2,))
            other.close()

        self.assertEqual(migrate(c, migrations=MIGRATIONS + (V2,), before=other_process_migrates_first), [])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM schema_version WHERE version = 2").fetchone()[0], 1)

    def test_foreign_database_refused(self):
        raw = sqlite3.connect(self.db)
        raw.execute("CREATE TABLE something (x)")
        raw.commit()
        raw.close()
        with self.assertRaises(UnknownDatabase):
            migrate(self.con())

    def test_too_new_refused(self):
        c = self.con()
        migrate(c)
        c.guard.migrating = True
        c.execute("INSERT INTO schema_version VALUES (99, 'будущее', '2030-01-01T00:00:00Z')")
        c.guard.migrating = False
        with self.assertRaises(SchemaTooNew):
            migrate(c)
        with self.assertRaises(SchemaTooNew):
            Store.open(self.db, readonly=True)

    def test_readonly_open_requires_current_format(self):
        sqlite3.connect(self.db).close()  # пустой файл = v0
        with self.assertRaises(SchemaOutdated):
            Store.open(self.db, readonly=True)


if __name__ == "__main__":
    unittest.main()
