"""Неизменяемость на уровне базы (S04): UPDATE/DELETE по журналу и архиву отклоняются самой SQLite,
даже если писать «сырым» SQL в обход API хранилища."""
import sqlite3
import unittest

from iam.storage import Snapshot, connect

from .helpers import EXP, PID, T0, TmpDirCase, ev, mem


class AppendOnlyTest(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.s = self.new_store()
        self.e1, _ = self.s.append_event(ev(1))
        self.s.append_event(ev(2))
        self.con = self.s.con

    def rejected(self, sql, *args, exc=sqlite3.IntegrityError):
        with self.assertRaises(exc, msg=sql):
            self.con.execute(sql, args)

    def test_update_and_delete_on_events_rejected(self):
        self.rejected("UPDATE events SET payload = '{}' WHERE seq = 1")
        self.rejected("UPDATE events SET seq = 10 WHERE seq = 2")
        self.rejected("DELETE FROM events WHERE seq = 2")
        self.rejected("DELETE FROM events")
        self.assertEqual(self.s.get_event(1), self.e1)

    def test_replace_and_upsert_cannot_bypass(self):
        row = self.con.execute("SELECT * FROM events WHERE seq = 1").fetchone()
        cols = row.keys()
        vals = list(row)
        vals[cols.index("seq")] = 3
        vals[cols.index("payload")] = '{"подмена":1}'
        sql = f"INSERT OR REPLACE INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        self.rejected(sql, *vals)
        upsert = (f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                  "ON CONFLICT (event_id) DO UPDATE SET payload = excluded.payload")
        vals[cols.index("seq")] = 3
        self.rejected(upsert, *vals)
        self.assertEqual(self.s.get_event(1).payload, {"n": 1})

    def test_seq_gap_and_foreign_persona_rejected(self):
        row = dict(self.con.execute("SELECT * FROM events WHERE seq = 1").fetchone())
        row.update(seq=5, event_id="evt_GAPGAPGAP", idempotency_key="rx:tg:gap")
        sql = f"INSERT INTO events ({','.join(row)}) VALUES ({','.join('?' * len(row))})"
        self.rejected(sql, *row.values())
        row.update(seq=None)
        self.rejected(sql, *row.values())
        row.update(seq=3, persona_id=EXP)
        self.rejected(sql, *row.values())

    def test_schema_changes_denied_on_core_connections(self):
        for sql in ("DROP TRIGGER events_no_update", "DROP TABLE events", "ALTER TABLE events ADD COLUMN x TEXT",
                    "DROP INDEX events_type", "DROP VIEW archive", "PRAGMA writable_schema = ON"):
            self.rejected(sql, exc=sqlite3.DatabaseError)
        self.assertEqual(self.con.execute("PRAGMA writable_schema").fetchone()[0], 0)

    def test_snapshots_immutable(self):
        self.s.put_snapshot(Snapshot(1, 1, 2, 20, T0, {"a": 1}))
        self.rejected("UPDATE state_snapshots SET state = '{}'")
        self.rejected("DELETE FROM state_snapshots")
        self.rejected("INSERT OR REPLACE INTO state_snapshots VALUES (1, 1, 2, 20, ?, '{}')", T0)

    def test_archive_layer_immutable_personal_mutable(self):
        a = mem("архивное", layer="archive")
        p = mem("личное")
        self.s.add_memory(a)
        self.s.add_memory(p)
        self.rejected("UPDATE memory_records SET text = 'x' WHERE memory_id = ?", a.memory_id)
        self.rejected("DELETE FROM memory_records WHERE memory_id = ?", a.memory_id)
        self.rejected("UPDATE memory_records SET layer = 'archive' WHERE memory_id = ?", p.memory_id)
        self.rejected("UPDATE memory_records SET layer = 'personal' WHERE memory_id = ?", a.memory_id)
        self.rejected("INSERT OR REPLACE INTO memory_records (memory_id, persona_id, layer, kind, origin, text,"
                      " version, record) VALUES (?, ?, 'personal', 'episodic', 'observation', 'x', 1, '{}')",
                      a.memory_id, PID)
        self.con.execute("UPDATE memory_records SET text = 'новое' WHERE memory_id = ?", (p.memory_id,))
        self.con.execute("DELETE FROM memory_records WHERE memory_id = ?", (p.memory_id,))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM archive").fetchone()[0], 1)

    def test_meta_identity_fixed(self):
        self.rejected("UPDATE meta SET value = ? WHERE key = 'persona_id'", EXP)
        self.rejected("UPDATE meta SET value = 'experimental' WHERE key = 'store_kind'")
        self.rejected("DELETE FROM meta WHERE key = 'persona_id'")
        self.rejected("INSERT INTO meta VALUES ('persona_id', ?)", EXP)
        self.con.execute("UPDATE meta SET value = 'x' WHERE key = 'created_at'")

    def test_inbox_immutable(self):
        self.con.execute("INSERT INTO inbox VALUES ('inp_AAAAAAAA', 'tg', '1', 1, ?, 'u', '{}')", (T0,))
        self.rejected("UPDATE inbox SET content = '{\"x\":1}'")
        self.rejected("DELETE FROM inbox")
        self.rejected("INSERT OR REPLACE INTO inbox VALUES ('inp_BBBBBBBB', 'tg', '1', 2, ?, 'u', '{}')", T0)

    def test_outbox_status_machine(self):
        ins = "INSERT INTO outbox (action_id, channel, recipient, content, status, requested_seq) VALUES (?, 'tg', 'u', '{}', ?, 1)"
        self.rejected(ins, "act_AAAAAAAA", "sent")
        self.con.execute(ins, ("act_AAAAAAAA", "requested"))
        self.rejected(ins, "act_AAAAAAAA", "requested")
        self.rejected("UPDATE outbox SET content = '{\"x\":1}'")
        self.rejected("UPDATE outbox SET status = 'requested'")
        self.con.execute("UPDATE outbox SET status = 'unknown', result_seq = 2")
        self.rejected("UPDATE outbox SET status = 'unknown'")
        self.con.execute("UPDATE outbox SET status = 'sent'")
        self.rejected("UPDATE outbox SET status = 'failed'")
        self.rejected("DELETE FROM outbox")

    def test_clock_monotonic(self):
        self.assertEqual(self.s.clock(), (20, T0))
        self.rejected("UPDATE clock SET life_time_ms = 5")
        self.rejected("DELETE FROM clock")
        self.s.heartbeat(5, "2026-10-11T18:00:01Z")  # меньшее значение не откатывает часы
        self.assertEqual(self.s.clock(), (20, "2026-10-11T18:00:01Z"))
        self.s.heartbeat(100, "2026-10-11T18:00:02Z")
        self.assertEqual(self.s.clock()[0], 100)

    def test_simulation_never_in_permanent_store(self):
        self.rejected("INSERT INTO memory_records (memory_id, persona_id, layer, kind, origin, text, version, record)"
                      " VALUES ('mem_SIMSIMSIM', ?, 'personal', 'episodic', 'simulation_event', 'x', 1, '{}')", PID)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.add_memory(mem("испытание", origin="simulation_event"))
        exp = self.new_store(EXP, self.tmp / "exp.db")
        exp.add_memory(mem("испытание", origin="simulation_event", persona=EXP))
        self.assertEqual(exp.store_kind, "experimental")

    def test_raw_connection_without_api_is_still_guarded_by_triggers(self):
        raw = sqlite3.connect(self.db)
        self.addCleanup(raw.close)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM events")
        guarded = connect(self.db)
        self.addCleanup(guarded.close)
        with self.assertRaises(sqlite3.DatabaseError):
            guarded.execute("DROP TABLE events")


if __name__ == "__main__":
    unittest.main()
