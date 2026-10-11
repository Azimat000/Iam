"""Нехватка места (R-205): запас проверяется до записи; при нехватке сеанс останавливается чисто, база цела."""
import errno
import sqlite3
import unittest

from iam.backup import (DEFAULT_RESERVE, NotEnoughSpace, SpaceGuard, guarded_call, is_disk_full, stop_on_low_space,
                        verify_database)
from iam.state import EventSpec, verify

from .helpers import BackupCase, set_


def fake_usage(free):
    return lambda path: (1_000_000, 1_000_000 - free, free)


class SpaceGuardTest(unittest.TestCase):
    def test_ensure_requires_reserve_plus_need(self):
        g = SpaceGuard("/", reserve=100, usage=fake_usage(500))
        self.assertEqual(g.ensure(400), 500)
        with self.assertRaises(NotEnoughSpace) as cm:
            g.ensure(401)
        self.assertEqual((cm.exception.free, cm.exception.need), (500, 501))
        self.assertEqual(g.free(), 500)

    def test_default_reserve_and_validation(self):
        self.assertEqual(SpaceGuard("/").reserve, DEFAULT_RESERVE)
        with self.assertRaises(ValueError):
            SpaceGuard("/", reserve=-1)

    def test_nonexistent_path_checks_nearest_existing_parent(self):
        seen = []
        g = SpaceGuard("/нет/такого/каталога", reserve=0, usage=lambda p: seen.append(p) or (1, 1, 1))
        g.ensure(0)
        self.assertEqual(seen, ["/"])

    def test_is_disk_full(self):
        self.assertTrue(is_disk_full(NotEnoughSpace(1, 2)))
        self.assertTrue(is_disk_full(OSError(errno.ENOSPC, "no space")))
        self.assertTrue(is_disk_full(OSError(errno.EDQUOT, "quota")))
        self.assertFalse(is_disk_full(OSError(errno.EACCES, "denied")))
        self.assertTrue(is_disk_full(sqlite3.OperationalError("database or disk is full")))
        self.assertFalse(is_disk_full(sqlite3.OperationalError("database is locked")))
        self.assertFalse(is_disk_full(ValueError("x")))


class CleanStopTest(BackupCase):
    def setUp(self):
        super().setUp()
        self.c = self.persona()

    def snapshot_of_truth(self):
        c = self.c
        return (c.epoch, c.state_version, c.state, c.store.last_seq())

    def test_low_space_before_write_stops_the_session_and_keeps_the_database(self):
        c = self.c
        before = self.snapshot_of_truth()
        wrote = []
        guard = SpaceGuard(self.live, reserve=10, usage=fake_usage(5))
        with self.assertRaises(NotEnoughSpace):
            guarded_call(c, guard, lambda: wrote.append(c.apply(self.prop(c, set_("traits", "curious", {"value": 1})))))
        self.assertEqual(wrote, [], "запись даже не начиналась")
        self.assertEqual(c.session_state, "stopped")
        self.assertEqual(c.epoch, before[0])
        self.assertEqual(c.state["traits"], before[2]["traits"], "состояние личности не тронуто")
        err = [e for e in c.store.events() if e.type == "system.error"][-1]
        self.assertEqual(err.payload["fault"], "disk_full")
        self.assertEqual(verify(c.store), [])
        self.assertEqual(c.store.integrity_problems(), [])
        self.assertTrue(verify_database(self.db).ok)

    def test_enough_space_runs_the_call_normally(self):
        c = self.c
        guard = SpaceGuard(self.live, reserve=10, usage=fake_usage(500))
        res = guarded_call(c, guard, lambda: c.apply(self.prop(c, set_("traits", "curious", {"value": 0.8}))), need=100)
        self.assertTrue(res.applied)
        self.assertEqual(c.session_state, "running")

    def test_disk_full_in_the_middle_of_a_write_rolls_back_and_stops(self):
        """Настоящий SQLITE_FULL: лимит страниц SQLite (имитация заполненного диска). Большая запись откатывается
        целиком, небольшая запись остановки умещается, сеанс останавливается, журнал воспроизводится."""
        c = self.c
        con = c.store.con
        pages = con.execute("PRAGMA page_count").fetchone()[0]
        before = self.snapshot_of_truth()
        con.execute(f"PRAGMA max_page_count = {pages}")
        big = {"text": "я" * 400_000}
        guard = SpaceGuard(self.live, reserve=10, usage=fake_usage(10**9))
        with self.assertRaises(sqlite3.OperationalError) as cm:
            guarded_call(c, guard, lambda: c.record_event(EventSpec("input.arrived", "rx:tg:big", big)))
        self.assertTrue(is_disk_full(cm.exception))
        self.assertEqual(c.session_state, "stopped")
        con.execute("PRAGMA max_page_count = 1073741823")      # «место освободили»
        self.assertIsNone(c.store.find_event(key="rx:tg:big"), "половинчатая запись не осталась")
        self.assertEqual(c.state["traits"], before[2]["traits"])
        self.assertEqual(verify(c.store), [])
        self.assertEqual(c.store.integrity_problems(), [])
        self.assertTrue(verify_database(self.db).ok)

    def test_stop_itself_may_fail_without_corruption(self):
        """Места нет даже на запись остановки: откат, ничего не записано, исключение «нет места» проглочено."""
        c = self.c
        con = c.store.con
        con.execute(f"PRAGMA max_page_count = {con.execute('PRAGMA page_count').fetchone()[0]}")
        # заполняем свободные страницы файла, чтобы небольшая запись тоже не умещалась
        filler = 0
        for size in (3000, 300, 3):      # сначала крупно, потом мелко: выбираем и остатки страниц
            while True:
                try:
                    c.record_event(EventSpec("input.arrived", f"rx:tg:fill{filler}", {"text": "я" * size}))
                except sqlite3.OperationalError:
                    break
                filler += 1
                self.assertLess(filler, 20000)
        last = c.store.last_seq()
        out = stop_on_low_space(c, NotEnoughSpace(0, 1))
        self.assertEqual((out.error_recorded, out.session_stopped), (False, False))
        self.assertEqual(c.store.last_seq(), last)
        con.execute("PRAGMA max_page_count = 1073741823")
        self.assertEqual(verify(c.store), [])
        self.assertTrue(verify_database(self.db).ok)

    def test_other_errors_are_not_swallowed(self):
        c = self.c
        guard = SpaceGuard(self.live, reserve=0, usage=fake_usage(10**9))
        with self.assertRaises(KeyError):
            guarded_call(c, guard, lambda: {}["нет"])
        self.assertEqual(c.session_state, "running", "обычная ошибка сеанс не останавливает")

    def test_stop_on_low_space_for_stopped_and_paused_sessions(self):
        c = self.c
        c.pause_session("operator")
        out = stop_on_low_space(c, NotEnoughSpace(1, 2))
        self.assertEqual((out.error_recorded, out.session_stopped, c.session_state), (True, True, "stopped"))
        out = stop_on_low_space(c, NotEnoughSpace(1, 2))
        self.assertEqual((out.error_recorded, out.session_stopped), (True, False), "уже остановлен")

    def test_foreign_errors_inside_stop_are_raised(self):
        c = self.c

        class Broken:
            session_state = "running"
            def record_event(self, spec):
                raise RuntimeError("не про место")
        with self.assertRaises(RuntimeError):
            stop_on_low_space(Broken(), NotEnoughSpace(1, 2))

        class BrokenStop(Broken):
            def record_event(self, spec):
                return c.record_event(spec)
            def stop_session(self):
                raise RuntimeError("не про место")
        with self.assertRaises(RuntimeError):
            stop_on_low_space(BrokenStop(), NotEnoughSpace(1, 2))
