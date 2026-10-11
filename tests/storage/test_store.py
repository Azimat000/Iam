"""API хранилища (S04): одна личность — один файл, журнал с идемпотентностью, снимки, память с FTS5,
входящие/исходящие, транзакции, проверка окружения."""
import sqlite3
import unittest
from dataclasses import replace

from iam.domain import ValidationError
from iam.storage import (IdempotencyConflict, InboxItem, NotFound, PersonaMismatch, RuntimeUnsupported, Snapshot,
                         StorageError, Store, check_runtime, connect, default_path, migrate, normalize_text,
                         runtime_problems, transaction)
from iam.storage.runtime import has_fts5

from .helpers import EXP, PID, T0, TmpDirCase, ev, mem


class RuntimeTest(unittest.TestCase):
    def test_current_environment_ok(self):
        self.assertEqual(runtime_problems(), [])
        check_runtime()
        self.assertTrue(has_fts5())

    def test_old_python_and_missing_fts5_reported(self):
        class NoFts:
            def __init__(self, *_):
                self.con = sqlite3.connect(":memory:")

            def execute(self, sql):
                raise sqlite3.OperationalError("no such module: fts5")

            def close(self):
                self.con.close()

        probs = runtime_problems((3, 12, 0), connect=NoFts)
        self.assertEqual(len(probs), 2)
        self.assertIn("3.12", probs[0])
        self.assertIn("FTS5", probs[1])
        with self.assertRaises(RuntimeUnsupported):
            check_runtime((3, 11), connect=NoFts)


class OpenCreateTest(TmpDirCase):
    def test_default_paths_separate_permanent_and_experimental(self):
        self.assertEqual(default_path(self.tmp, PID), self.tmp / "personas" / f"{PID}.db")
        self.assertEqual(default_path(self.tmp, EXP), self.tmp / "experiments" / f"{EXP}.db")

    def test_create_and_reopen(self):
        p = default_path(self.tmp, PID)
        s = Store.create(p, PID, created_at=T0)
        self.assertEqual((s.persona_id, s.store_kind, s.created_at), (PID, "permanent", T0))
        self.assertFalse(s.readonly)
        s.close()
        with Store.open(p, expect_persona=PID) as s2:
            self.assertEqual(s2.persona_id, PID)
        with self.assertRaises(PersonaMismatch):
            Store.open(p, expect_persona=EXP)
        Store.open(p).close()  # замок отпущен даже после отказа

    def test_one_file_one_persona(self):
        self.new_store().close()
        with self.assertRaises(StorageError) as cm:
            Store.create(self.db, EXP)
        self.assertIn("уже существует", str(cm.exception))
        with Store.open(self.db) as s:
            self.assertEqual(s.persona_id, PID)

    def test_create_is_atomic(self):
        """Сбой посреди создания не оставляет полуготового файла; следующий create работает."""
        from unittest import mock
        with mock.patch("iam.storage.store.migrate", side_effect=RuntimeError("сбой")):
            with self.assertRaises(RuntimeError):
                Store.create(self.db, PID)
        self.assertFalse(self.db.exists())
        stale = self.tmp / ".p.db.creating"
        self.assertTrue(stale.exists(), "остался только черновик под другим именем")
        with Store.create(self.db, PID) as s:
            self.assertEqual(s.persona_id, PID)
        self.assertFalse(stale.exists(), "черновик убран")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith(".p.db")), [])

    def test_create_refuses_existing_file_and_bad_id(self):
        sqlite3.connect(self.db).execute("CREATE TABLE x (y)").connection.close()
        with self.assertRaises(StorageError):
            Store.create(self.db, PID)
        with self.assertRaises(ValueError):
            Store.create(self.tmp / "q.db", "persona-1")
        Store.create(self.tmp / "q.db", PID).close()  # замок после отказа отпущен

    def test_open_missing_and_incomplete(self):
        with self.assertRaises(NotFound):
            Store.open(self.tmp / "none.db")
        c = connect(self.db)
        migrate(c)
        c.close()
        with self.assertRaises(StorageError):
            Store.open(self.db)
        with self.assertRaises(StorageError):
            Store.open(self.db, readonly=True)

    def test_readonly_cannot_write(self):
        self.new_store().append_event(ev(1))
        with Store.open(self.db, readonly=True) as ro:
            self.assertTrue(ro.readonly)
            self.assertEqual(ro.last_seq(), 1)
            for call in (lambda: ro.append_event(ev(2)), lambda: ro.heartbeat(1, T0),
                         lambda: ro.add_memory(mem("x")), lambda: ro.update_memory(mem("x")),
                         lambda: ro.put_snapshot(Snapshot(1, 1, 1, 0, T0, {})),
                         lambda: ro.add_outbox("act_AAAAAAAA", "tg", "u", {}, 1),
                         lambda: ro.set_outbox_status("act_AAAAAAAA", "sent", 1),
                         lambda: ro.add_inbox(InboxItem("inp_AAAAAAAA", "tg", "1", 1, T0, "u", {})),
                         lambda: ro.transaction().__enter__()):
                with self.assertRaises(StorageError):
                    call()
            self.assertEqual(ro.integrity_problems(), [])


class JournalTest(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.s = self.new_store()

    def test_seq_assigned_in_order_without_gaps(self):
        got = [self.s.append_event(ev(i))[0].seq for i in range(1, 11)]
        self.assertEqual(got, list(range(1, 11)))
        self.assertEqual([e.seq for e in self.s.events()], got)
        self.assertEqual([e.seq for e in self.s.events(3, 5)], [3, 4, 5])
        self.assertEqual({e.persona_id for e in self.s.events()}, {PID}, "в базе один persona_id")

    def test_state_versions_monotonic_in_one_sequence(self):
        """R-024 (часть хранилища): снимки одной личности читаются одной линией по версиям.
        Непрерывность версий (без пропусков) обеспечивает StateController (S05)."""
        for v in range(1, 6):
            e, _ = self.s.append_event(ev(v, type_="memory.recorded", version=v))
            self.s.put_snapshot(Snapshot(1, v, e.seq, e.life_time_ms, T0, {"v": v}))
        rows = self.s.con.execute("SELECT state_version FROM state_snapshots ORDER BY epoch, state_version").fetchall()
        self.assertEqual([r[0] for r in rows], [1, 2, 3, 4, 5])
        self.assertEqual(self.s.latest_snapshot().state, {"v": 5})
        self.assertEqual(self.s.get_snapshot(1, 2).at_seq, 2)
        with self.assertRaises(NotFound):
            self.s.get_snapshot(1, 99)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.put_snapshot(Snapshot(1, 5, 5, 0, T0, {}))

    def test_latest_snapshot_none_on_fresh(self):
        self.assertIsNone(self.s.latest_snapshot())
        self.assertIsNone(self.s.clock())

    def test_roundtrip_preserves_event(self):
        e = ev(7, causes=("evt_ABCDEFGH",), payload={"текст": "привет", "вложено": {"a": [1, 2]}})
        stored, created = self.s.append_event(e)
        self.assertTrue(created)
        self.assertEqual(stored, e.with_seq(1))
        self.assertEqual(self.s.get_event(1), stored)
        self.assertEqual(self.s.find_event(event_id=e.event_id), stored)
        self.assertEqual(self.s.find_event(key="rx:tg:7"), stored)
        self.assertIsNone(self.s.find_event(key="rx:tg:nope"))
        with self.assertRaises(ValueError):
            self.s.find_event()
        with self.assertRaises(ValueError):
            self.s.find_event(event_id="a", key="b")
        with self.assertRaises(NotFound):
            self.s.get_event(99)

    def test_idempotent_redelivery(self):
        first, created = self.s.append_event(ev(1))
        again, created2 = self.s.append_event(ev(1))  # новая доставка того же: другой event_id, тот же ключ
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(again, first)
        self.assertEqual(self.s.last_seq(), 1)

    def test_same_key_other_payload_is_conflict(self):
        self.s.append_event(ev(1))
        with self.assertRaises(IdempotencyConflict) as cm:
            self.s.append_event(ev(1, payload={"n": "другое"}))
        self.assertEqual((cm.exception.key, cm.exception.existing_seq), ("rx:tg:1", 1))
        self.assertEqual(self.s.get_event(1).payload, {"n": 1}, "оригинал не перезаписан")

    def test_rejects_foreign_invalid_and_preassigned(self):
        with self.assertRaises(PersonaMismatch):
            self.s.append_event(ev(1, persona=EXP))
        with self.assertRaises(ValidationError):
            self.s.append_event(ev(1, key="bad"))
        with self.assertRaises(ValueError):
            self.s.append_event(ev(1).with_seq(1))
        self.assertEqual(self.s.last_seq(), 0)

    def test_events_update_clock_point(self):
        self.s.append_event(ev(1, life=500))
        self.s.append_event(ev(2, life=300))
        self.assertEqual(self.s.clock()[0], 500)

    def test_transaction_groups_writes(self):
        with self.s.transaction():
            with self.s.transaction():  # вложенный блок входит во внешний
                self.s.append_event(ev(1))
            self.s.append_event(ev(2))
        self.assertEqual(self.s.last_seq(), 2)
        with self.s.transaction():
            try:
                with self.s.transaction():
                    self.s.append_event(ev(10))
                    raise KeyError("перехвачено внешним блоком")
            except KeyError:
                pass
            self.s.append_event(ev(11))
        self.assertIsNone(self.s.find_event(key="rx:tg:10"), "вложенный блок откатился целиком")
        self.assertEqual(self.s.get_event(3).idempotency_key, "rx:tg:11", "внешний — зафиксирован, seq без пропуска")
        with self.assertRaises(KeyError):
            with self.s.transaction():
                self.s.append_event(ev(3))
                with self.s.transaction():
                    self.s.append_event(ev(4))
                    raise KeyError("сбой во вложенном блоке")
        self.assertEqual(self.s.last_seq(), 3, "откатилось всё, включая внешний блок")
        self.assertFalse(self.s.con.in_transaction)

    def test_db_transaction_helper(self):
        c = self.s.con
        with transaction(c):
            self.assertTrue(c.in_transaction)
        self.assertFalse(c.in_transaction)

    def test_commit_failure_rolls_back_and_frees_connection(self):
        c = connect(self.tmp / "fk.db")
        self.addCleanup(c.close)
        c.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        c.execute("CREATE TABLE child (pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
        with self.assertRaises(sqlite3.IntegrityError):  # отложенный внешний ключ проверяется при COMMIT
            with transaction(c):
                c.execute("INSERT INTO child VALUES (42)")
        self.assertFalse(c.in_transaction, "соединение не осталось в транзакции")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM child").fetchone()[0], 0)
        with transaction(c):
            c.execute("INSERT INTO parent VALUES (1)")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM parent").fetchone()[0], 1)

    def test_already_rolled_back_transaction_keeps_original_error(self):
        c = self.s.con
        for nested in (False, True):
            with self.assertRaises(ZeroDivisionError):
                with transaction(c):
                    if nested:
                        with transaction(c):
                            c.execute("ROLLBACK")  # как если бы SQLite откатил сам
                            1 / 0
                    c.execute("ROLLBACK")
                    1 / 0
            self.assertFalse(c.in_transaction)


class MemoryTest(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.s = self.new_store()

    def test_add_get_update(self):
        r = mem("Сегодня читала про звёзды", availability=0.9)
        self.s.add_memory(r)
        self.assertEqual(self.s.get_memory(r.memory_id), r)
        faded = replace(r, availability=0.4)
        self.s.update_memory(faded)
        self.assertEqual(self.s.get_memory(r.memory_id).availability, 0.4)
        revised = replace(r, content={"text": "Читала про кометы"}, version=2)
        self.s.update_memory(revised)
        self.assertEqual(self.s.search_memory("кометы"), [revised])
        self.assertEqual(self.s.search_memory("звёзды"), [], "индекс обновлён вместе с текстом")
        with self.assertRaises(NotFound):
            self.s.get_memory("mem_NOPENOPE")

    def test_update_rules(self):
        a = mem("архив", layer="archive")
        p = mem("личное", version=2)
        self.s.add_memory(a)
        self.s.add_memory(p)
        with self.assertRaises(StorageError):
            self.s.update_memory(replace(a, content={"text": "подмена"}))
        with self.assertRaises(StorageError):
            self.s.update_memory(replace(p, layer="interpretation"))
        with self.assertRaises(StorageError):
            self.s.update_memory(replace(p, persona_id=EXP))
        with self.assertRaises(StorageError):
            self.s.update_memory(replace(p, version=1))
        with self.assertRaises(NotFound):
            self.s.update_memory(mem("нет такой"))
        with self.assertRaises(PersonaMismatch):
            self.s.add_memory(mem("чужая", persona=EXP))
        with self.assertRaises(ValidationError):
            self.s.add_memory(mem("x", origin="stated_by_other"))  # нет группы независимости

    def test_fts_russian_case_insensitive(self):
        a = mem("Оператор рассказал про КОШКУ Мурку")
        b = mem("Ёлка во дворе зажглась вечером")
        c = mem("кошку снова видела у подъезда", layer="interpretation")
        for r in (a, b, c):
            self.s.add_memory(r)
        self.assertEqual({r.memory_id for r in self.s.search_memory("кошку")}, {a.memory_id, c.memory_id})
        self.assertEqual(self.s.search_memory("ОПЕРАТОР кошку"), [a])
        self.assertEqual(self.s.search_memory("мурку"), [a])
        self.assertEqual(self.s.search_memory("елка"), [b], "ё и е не различаются")
        self.assertEqual(self.s.search_memory("ЁЛКА"), [b])
        self.assertEqual(self.s.search_memory("кошку", layers=("interpretation",)), [c])
        self.assertEqual(self.s.search_memory("собака"), [])
        self.assertEqual(self.s.search_memory("  ... "), [])
        self.assertEqual(len(self.s.search_memory("кошку", limit=1)), 1)
        self.assertEqual(self.s.search_memory('"кошку" OR NEAR'), [], "синтаксис FTS из запроса не исполняется")

    def test_archive_invisible_to_ordinary_search(self):
        arch = mem("тайное воспоминание в архиве", layer="archive")
        self.s.add_memory(arch)
        self.assertEqual(self.s.search_memory("тайное"), [])
        self.assertEqual(self.s.search_memory("тайное", layers=("archive",)), [arch])
        for bad in ((), ("nope",)):
            with self.assertRaises(ValueError):
                self.s.search_memory("x", layers=bad)

    def test_normalize_text(self):
        self.assertEqual(normalize_text("Ёж и ёлка"), "Еж и елка")


class InOutTest(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.s = self.new_store()
        self.s.append_event(ev(1))
        self.s.append_event(ev(2))

    def test_inbox_dedup_by_channel_and_external_id(self):
        item = InboxItem("inp_AAAAAAAA", "telegram", "100500", 1, T0, "u_111", {"text": "привет"})
        self.assertEqual(self.s.add_inbox(item), (item, True))
        dup = replace(item, input_id="inp_BBBBBBBB", arrived_seq=2)
        self.assertEqual(self.s.add_inbox(dup), (item, False))
        self.assertEqual(self.s.get_inbox("inp_AAAAAAAA"), item)
        self.assertEqual(self.s.find_inbox("telegram", "100500"), item)
        self.assertIsNone(self.s.find_inbox("telegram", "1"))
        with self.assertRaises(NotFound):
            self.s.get_inbox("inp_BBBBBBBB")

    def test_outbox_lifecycle(self):
        self.s.add_outbox("act_AAAAAAAA", "telegram", "u_111", {"text": "ответ"}, 1)
        o = self.s.get_outbox("act_AAAAAAAA")
        self.assertEqual((o.status, o.result_seq, o.content), ("requested", None, {"text": "ответ"}))
        self.s.set_outbox_status("act_AAAAAAAA", "unknown", 2)
        self.s.set_outbox_status("act_AAAAAAAA", "sent", 2, external_id="m1")
        o = self.s.get_outbox("act_AAAAAAAA")
        self.assertEqual((o.status, o.external_id), ("sent", "m1"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.set_outbox_status("act_AAAAAAAA", "failed", 2)
        with self.assertRaises(NotFound):
            self.s.set_outbox_status("act_NOPENOPE", "sent", 2)


class IntegrityTest(TmpDirCase):
    def test_reports_problems(self):
        s = self.new_store()
        s.append_event(ev(1))
        self.assertEqual(s.integrity_problems(), [])
        # Искусственная порча в обход защит (режим миграции снимает сторожа): пропуск seq.
        s.con.guard.migrating = True
        s.con.execute("DROP TRIGGER events_seq_contiguous")
        row = dict(s.con.execute("SELECT * FROM events").fetchone())
        row.update(seq=5, event_id="evt_GAPGAPGAP", idempotency_key="rx:tg:gap")
        s.con.execute(f"INSERT INTO events ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                      tuple(row.values()))
        s.con.guard.migrating = False
        self.assertIn("пропуски seq", s.integrity_problems()[0])

    def test_reports_page_and_fts_errors(self):
        s = self.new_store()
        real = s.con.execute

        def fake(sql, *a):
            if sql.startswith("PRAGMA integrity_check"):
                return iter([("страница 7 повреждена",)])
            if "integrity-check" in sql:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return real(sql, *a)

        s.con.execute = fake
        probs = s.integrity_problems()
        s.con.execute = real
        self.assertEqual(len(probs), 2)
        self.assertIn("страница 7", probs[0])
        self.assertIn("FTS", probs[1])


if __name__ == "__main__":
    unittest.main()
