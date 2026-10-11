"""Восстановление копии (R-187, R-203; сценарий scenarios/required/restore_backup.toml): только в отдельный каталог,
эпоха растёт, восстановление отмечено, опыт после копии не остаётся, ответ из прежней эпохи устаревает."""
import hashlib
import sqlite3
import unittest
from unittest import mock

from iam.backup import (BackupCorrupt, NotEnoughSpace, RestoreRefused, SpaceGuard, check_backup, create_backup,
                        restore_backup)
from iam.backup import restore as restore_mod
from iam.domain import Op, new_id
from iam.state import EventSpec, StateController, mark_restored, replay, verify
from iam.storage import Store

from state.test_response import ResponseCase

from .helpers import BackupCase, PID, T0, memory, set_


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RestoreScenarioTest(BackupCase):
    def setUp(self):
        super().setUp()
        self.target = self.tmp / "restored"

    def prepare(self):
        """Личность → копия → жизнь после копии. Возвращает (контроллер, результат копии)."""
        c = self.persona()
        bk = create_backup(self.db, self.backups, now=self.tick(), controller=c)
        self.live_more(c)
        return c, bk

    def test_scenario_restore_backup(self):
        c, bk = self.prepare()
        live_last = c.store.last_seq()
        live_state = c.state
        live_epoch = c.epoch
        res = restore_backup(bk.info.path, self.target, reason="Проверка: вернуть копию до главы 2",
                             live_db=self.db, now=self.tick())

        self.assertEqual(res.path, self.target / f"{PID}.db")
        self.assertEqual((res.from_epoch, res.to_epoch, res.checked_on_copy), (1, 2, True))
        self.assertEqual((res.restored_to_seq, res.lost_seq_from, res.lost_seq_to),
                         (bk.info.manifest["last_seq"], bk.info.manifest["last_seq"] + 1, live_last))

        with Store.open(res.path) as store:
            self.assertEqual(verify(store), [], "журнал воспроизводится через смену эпохи")
            self.assertEqual(store.integrity_problems(), [])
            events = list(store.events())
            restored = [e for e in events if e.type == "system.restored"]
            self.assertEqual(len(restored), 1)
            r = restored[0]
            self.assertEqual((r.epoch, r.changes_state, r.payload["checked_on_copy"], r.payload["snapshot_id"],
                              r.idempotency_key), (2, True, True, bk.info.snapshot_id, f"rst:{bk.info.snapshot_id}"))
            cause = store.find_event(event_id=r.causes[0])
            self.assertEqual((cause.type, cause.payload["kind"], cause.source.kind),
                             ("operator.intervention", "restore_backup", "operator_panel"), "восстановление — вмешательство оператора")
            # то, что было до копии, на месте; опыта после копии нет ни в журнале, ни в состоянии
            text = " ".join(str(e.payload) for e in events)
            self.assertIn("прочитала главу 1", text)
            self.assertNotIn("прочитай главу 2", text)
            self.assertEqual(max(e.seq for e in events), r.seq)
            self.assertEqual(r.seq, bk.info.manifest["last_seq"] + 2)
            self.assertEqual(store.get_memory("mem_AAAAAAAA").content["text"], "До копии: она прочитала главу 1.")
            # событие о создании копии записано после неё и в копию не попало
            self.assertNotIn(bk.event_id, [e.event_id for e in events])

            ctl = StateController(store, clock=self.clock(initial=0), recover=False)
            self.assertEqual((ctl.epoch, ctl.state_version), (2, bk.info.manifest["state_version"]))
            self.assertEqual(ctl.state["traits"]["curious"]["value"], 0.6, "значение из копии, не из жизни после неё")
            self.assertNotEqual(live_state["traits"]["curious"]["value"], 0.6)
            self.assertEqual(live_epoch, 1)
            # после восстановления личность живёт дальше в новой эпохе
            self.assertEqual(ctl.session_state, "running", "состояние ровно такое, каким оно было в копии")
            self.assertTrue(ctl.apply(self.prop(ctl, set_("traits", "curious", {"value": 0.7}))).applied)
            self.assertEqual(verify(store), [])

    def test_live_database_is_untouched(self):
        c, bk = self.prepare()
        c.store.close()
        before = file_digest(self.db)
        restore_backup(bk.info.path, self.target, reason="проба", live_db=self.db)
        self.assertEqual(file_digest(self.db), before)
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), [f"{PID}.db"], "черновики убраны")

    def test_restored_copy_is_checked_before_and_after(self):
        c, bk = self.prepare()
        calls = []
        real = restore_mod.verify
        with mock.patch.object(restore_mod, "verify", side_effect=lambda s: calls.append(s.last_seq()) or real(s)):
            restore_backup(bk.info.path, self.target, reason="проба")
        self.assertEqual(calls, [bk.info.manifest["last_seq"], bk.info.manifest["last_seq"] + 2],
                         "проверка на отдельной копии до отметки и после неё")

    def test_old_model_answer_goes_stale_after_restore(self):
        """Ответ модели, выданный до отката, устаревает по эпохе, даже если номер версии совпал."""
        c = self.controller(running=True)
        req = {"request_id": new_id("req"), "persona_id": PID, "session_id": c.session_id, "epoch": c.epoch,
               "state_version": c.state_version, "issued_at": "2026-10-11T10:00:00.000Z", "life_time_ms": c.life_time_ms,
               "purpose": "choose_action", "context": [], "allowed_proposals": ["state_change", "no_action"],
               "allowed_actions": [], "limits": {"max_output_tokens": 500, "deadline_ms": 600000, "attempt": 0},
               "response_schema": "response.schema.json", "cycle_id": new_id("cyc")}
        req["idempotency_key"] = f"req:{req['request_id']}"
        self.assertEqual(c.record_request(req).status, "recorded")
        bk = create_backup(self.db, self.backups, now=self.tick())
        c.store.close()
        res = restore_backup(bk.info.path, self.target, reason="проверка устаревания")
        store = Store.open(res.path)
        self.addCleanup(store.close)
        ctl = StateController(store, clock=self.clock(), recover=False)
        before = ctl.state
        self.assertEqual((ctl.epoch, ctl.state_version), (2, req["state_version"]), "номер версии тот же, эпоха другая")
        answer = {"request_id": req["request_id"], "epoch": req["epoch"], "state_version": req["state_version"],
                  "provider": {"name": "stub", "model": "stub-1"}, "status": "ok", "explanation": "x",
                  "usage": {"calls": 1, "duration_ms": 1}, "tool_use_detected": False,
                  "proposals": [{"proposal_id": new_id("prop"), "kind": "state_change", "rationale": "r", "cites": [],
                                 "body": {"ops": [{"group": "traits", "path": "curious", "op": "set", "value": {"value": 0.99}}]}}]}
        out = ctl.receive_response(answer)
        self.assertEqual((out.status, out.reason), ("stale", "version"))
        self.assertEqual(ctl.state, before)
        self.assertEqual(sum(e.type == "proposal.stale" for e in store.events()), 1)

    def test_answer_for_a_request_lost_in_the_rollback_is_not_applied(self):
        """Запрос, выданный после копии, в восстановленном журнале не существует: ответ ничего не меняет."""
        c = self.persona()
        bk = create_backup(self.db, self.backups, now=self.tick())
        req = {"request_id": new_id("req"), "persona_id": PID, "session_id": c.session_id, "epoch": 1,
               "state_version": c.state_version, "issued_at": "2026-10-11T10:00:00.000Z", "life_time_ms": 0,
               "purpose": "choose_action", "context": [], "allowed_proposals": ["state_change"], "allowed_actions": [],
               "limits": {"max_output_tokens": 500, "deadline_ms": 600000, "attempt": 0},
               "response_schema": "response.schema.json", "cycle_id": new_id("cyc")}
        req["idempotency_key"] = f"req:{req['request_id']}"
        c.record_request(req)
        c.store.close()
        res = restore_backup(bk.info.path, self.target, reason="проверка")
        store = Store.open(res.path)
        self.addCleanup(store.close)
        ctl = StateController(store, clock=self.clock(), recover=False)
        before = ctl.state
        out = ctl.receive_response({
            "request_id": req["request_id"], "epoch": 1, "state_version": req["state_version"],
            "provider": {"name": "stub", "model": "stub-1"}, "status": "ok", "explanation": "x",
            "usage": {"calls": 1, "duration_ms": 1}, "tool_use_detected": False,
            "proposals": [{"proposal_id": new_id("prop"), "kind": "state_change", "rationale": "r", "cites": [],
                           "body": {"ops": [{"group": "traits", "path": "curious", "op": "set", "value": {"value": 0.99}}]}}]})
        self.assertNotEqual(out.status, "applied")
        self.assertEqual(ctl.state, before)

    def test_restore_of_a_personality_without_transitions(self):
        """Были только входящие, переходов состояния нет: линия снимков начинается с (1, 0), затем (2, 0)."""
        store = Store.create(self.db, PID)
        c = StateController(store, clock=self.clock())
        c.record_event(EventSpec("input.arrived", "rx:tg:1", {"text": "привет"}))
        store.close()
        bk = create_backup(self.db, self.backups, now=self.tick())
        res = restore_backup(bk.info.path, self.target, reason="личность без переходов")
        with Store.open(res.path) as s:
            self.assertEqual((res.from_epoch, res.to_epoch), (1, 2))
            self.assertEqual([(x.epoch, x.state_version) for x in (s.get_snapshot(1, 0), s.get_snapshot(2, 0))], [(1, 0), (2, 0)])
            self.assertEqual(verify(s), [])
            self.assertEqual(StateController(s, clock=self.clock()).epoch, 2)

    def test_database_without_events_cannot_be_restored(self):
        Store.create(self.db, PID).close()
        bk = create_backup(self.db, self.backups, now=self.tick())
        with self.assertRaises(RestoreRefused):
            restore_backup(bk.info.path, self.target, reason="совсем пустая")
        self.assertFalse(self.target.exists())


class RestoreRefusalTest(BackupCase):
    def setUp(self):
        super().setUp()
        self.target = self.tmp / "restored"
        c = self.persona()
        self.bk = create_backup(self.db, self.backups, now=self.tick())
        c.store.close()

    def assertNothingLeft(self):
        left = sorted(p.name for p in self.target.iterdir()) if self.target.exists() else []
        self.assertEqual(left, [])

    def test_reason_is_required(self):
        for reason in ("", "   ", None):
            with self.assertRaises(RestoreRefused):
                restore_backup(self.bk.info.path, self.target, reason=reason)
        self.assertFalse(self.target.exists())

    def test_existing_database_is_never_overwritten(self):
        restore_backup(self.bk.info.path, self.target, reason="первый раз")
        first = file_digest(self.target / f"{PID}.db")
        with self.assertRaises(RestoreRefused):
            restore_backup(self.bk.info.path, self.target, reason="второй раз")
        self.assertEqual(file_digest(self.target / f"{PID}.db"), first)

    def test_restore_over_the_live_database_is_refused(self):
        with self.assertRaises(RestoreRefused):
            restore_backup(self.bk.info.path, self.live, reason="поверх живой", live_db=self.db)
        with self.assertRaises(RestoreRefused):   # даже без live_db: файл личности уже есть
            restore_backup(self.bk.info.path, self.live, reason="поверх живой")

    def test_corrupt_backup_is_refused_before_any_write(self):
        raw = bytearray(self.bk.info.path.read_bytes())
        raw[len(raw) // 3] ^= 0xFF
        self.bk.info.path.write_bytes(raw)
        with self.assertRaises(BackupCorrupt):
            restore_backup(self.bk.info.path, self.target, reason="повреждённая")
        self.assertFalse(self.target.exists())

    def test_not_enough_space_is_refused_before_any_write(self):
        with self.assertRaises(NotEnoughSpace):
            restore_backup(self.bk.info.path, self.target, reason="мало места",
                           space=SpaceGuard(self.tmp, usage=lambda p: (1, 1, 1)))
        self.assertNothingLeft()

    def test_tampered_content_is_caught_by_the_backup_check_before_restore(self):
        """Копия цела по sha256 (манифест подогнан), но журнал не сходится со снимками: отказ до записи."""
        con = sqlite3.connect(self.bk.info.path)
        con.execute("DROP TRIGGER state_snapshots_no_update")
        con.execute("UPDATE state_snapshots SET state = replace(state, '0.6', '0.1') WHERE state_version = 3")
        con.commit()
        con.close()
        import json
        m = json.loads(self.bk.info.manifest_path.read_text(encoding="utf-8"))
        m["sha256"], m["size_bytes"] = file_digest(self.bk.info.path), self.bk.info.path.stat().st_size
        self.bk.info.manifest_path.write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(BackupCorrupt) as cm:
            restore_backup(self.bk.info.path, self.target, reason="подмена")
        self.assertIn("не совпадает", str(cm.exception))
        self.assertNothingLeft()

    def test_check_on_the_separate_copy_refuses_before_marking(self):
        """Проверка на отдельной копии (после возможной миграции формата) — отдельный рубеж."""
        real = restore_mod.verify
        n = []
        def first_fails(store):
            n.append(1)
            return ["журнал не сходится"] if len(n) == 1 else real(store)
        with mock.patch.object(restore_mod, "verify", side_effect=first_fails):
            with self.assertRaises(BackupCorrupt) as cm:
                restore_backup(self.bk.info.path, self.target, reason="проверка")
        self.assertIn("проверка на отдельной копии", str(cm.exception))
        self.assertNothingLeft()

    def test_copy_failure_before_the_store_opens_leaves_nothing(self):
        with mock.patch.object(restore_mod, "_copy_database", side_effect=OSError("сбой чтения")):
            with self.assertRaises(OSError):
                restore_backup(self.bk.info.path, self.target, reason="сбой")
        self.assertNothingLeft()

    def test_failed_check_after_marking_leaves_nothing(self):
        real = restore_mod.verify
        n = []
        def flaky(store):
            n.append(1)
            return real(store) if len(n) == 1 else ["что-то не так после отметки"]
        with mock.patch.object(restore_mod, "verify", side_effect=flaky):
            with self.assertRaises(BackupCorrupt) as cm:
                restore_backup(self.bk.info.path, self.target, reason="проверка")
        self.assertIn("после восстановления", str(cm.exception))
        self.assertNothingLeft()

    def test_unreadable_live_database_only_loses_the_gap_information(self):
        bad = self.tmp / "bad.db"
        bad.write_bytes(b"not a database " * 50)
        res = restore_backup(self.bk.info.path, self.target, reason="живая база нечитаема", live_db=bad)
        self.assertIsNone(res.lost_seq_to)
        res2 = restore_backup(self.bk.info.path, self.tmp / "other", reason="живой базы нет", live_db=self.tmp / "нет.db")
        self.assertIsNone(res2.lost_seq_to)

    def test_backup_listing_still_ok_after_restore(self):
        restore_backup(self.bk.info.path, self.target, reason="проба")
        self.assertTrue(check_backup(self.bk.info.path).ok)


class MarkRestoredTest(BackupCase):
    def make(self):
        self.persona().store.close()
        return Store.open(self.db)

    def test_requires_a_writable_store_and_a_reason(self):
        store = self.make()
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            mark_restored(store, snapshot_id="snp_AAAAAAAA", reason=" ", checked_on_copy=True, backup={})
        store.close()
        ro = Store.open(self.db, readonly=True)
        self.addCleanup(ro.close)
        with self.assertRaises(ValueError):
            mark_restored(ro, snapshot_id="snp_AAAAAAAA", reason="x", checked_on_copy=True, backup={})

    def test_second_restore_makes_epoch_three_and_replay_follows(self):
        store = self.make()
        self.addCleanup(store.close)
        mark_restored(store, snapshot_id="snp_AAAAAAAA", reason="раз", checked_on_copy=True, backup={"k": 1})
        mark_restored(store, snapshot_id="snp_BBBBBBBB", reason="два", checked_on_copy=False, backup={}, lost_seq_to=99)
        self.assertEqual(verify(store), [])
        epochs = [s.epoch for s in replay(store)]
        self.assertEqual(sorted(set(epochs)), [1, 2, 3])
        last = [e for e in store.events() if e.type == "system.restored"][-1]
        self.assertEqual((last.payload["from_epoch"], last.payload["to_epoch"], last.payload["checked_on_copy"]), (2, 3, False))
        self.assertEqual((last.payload["lost_seq_from"], last.payload["lost_seq_to"]), (last.payload["restored_to_seq"] + 1, 99))

    def test_lost_range_only_when_live_journal_was_longer(self):
        store = self.make()
        self.addCleanup(store.close)
        _, r = mark_restored(store, snapshot_id="snp_AAAAAAAA", reason="раз", checked_on_copy=True, backup={},
                             lost_seq_to=store.last_seq())
        self.assertNotIn("lost_seq_from", r.payload)

    def test_replay_detects_inconsistent_restore_event(self):
        store = self.make()
        mark_restored(store, snapshot_id="snp_AAAAAAAA", reason="раз", checked_on_copy=True, backup={})
        store.close()
        con = sqlite3.connect(self.db)
        con.execute("DROP TRIGGER events_no_update")
        con.execute("UPDATE events SET payload = replace(payload, '\"to_epoch\":2', '\"to_epoch\":5') WHERE type = 'system.restored'")
        con.commit()
        con.close()
        with Store.open(self.db, readonly=True) as ro:
            self.assertTrue(any("system.restored с эпохами" in p for p in verify(ro)))

    def test_store_without_events_is_refused(self):
        Store.create(self.tmp / "e.db", PID).close()
        with Store.open(self.tmp / "e.db") as s:
            with self.assertRaises(ValueError):
                mark_restored(s, snapshot_id="snp_AAAAAAAA", reason="x", checked_on_copy=True, backup={})
