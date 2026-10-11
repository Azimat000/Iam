"""Часы жизни (docs/spec/time.md#life-clock): идут только в сеансе, не убывают, не зависят от календаря."""
import unittest

from iam.clock import (ClockError, LifeClock, calendar_diff_ms, calendar_moment, calendar_now, due_moments, life_moment,
                       moment_errors, parse_calendar)
from iam.storage import Store

from .helpers import PID, StateCase, plain_event


class LifeClockTest(StateCase):
    def test_runs_only_while_resumed(self):
        c = self.clock(1000)
        self.assertEqual(c.now_ms(), 1000)
        self.time.advance(500)
        self.assertEqual(c.now_ms(), 1000, "на остановленных часах время стоит")
        self.assertEqual(c.resume(), 1000)
        self.time.advance(250)
        self.assertEqual(c.now_ms(), 1250)
        self.assertEqual(c.freeze(), 1250)
        self.time.advance(10_000)
        self.assertEqual(c.now_ms(), 1250)
        c.resume()
        self.time.advance(5)
        self.assertEqual(c.now_ms(), 1255)

    def test_calendar_jump_does_not_change_life_time(self):
        """Подмена календарных часов: скачок вперёд и назад жизненное время не трогает."""
        c = self.clock()
        c.resume()
        self.time.advance(100)
        self.time.jump_calendar(86_400_000)
        self.time.jump_calendar(-3 * 86_400_000)
        self.assertEqual(c.now_ms(), 100)

    def test_never_decreases_even_if_source_goes_back(self):
        c = self.clock()
        c.resume()
        self.time.advance(100)
        self.assertEqual(c.now_ms(), 100)
        self.time.mono_ns -= 60_000_000   # сбой источника: монотонные часы пошли назад
        self.assertEqual(c.now_ms(), 100)
        self.time.advance(80)
        self.assertEqual(c.now_ms(), 100 + 20, "после сбоя время продолжает идти вперёд от максимума")

    def test_time_scale(self):
        c = self.clock(time_scale=10)
        c.resume()
        self.time.advance(100)
        self.assertEqual(c.now_ms(), 1000)

    def test_freeze_at_recorded_value(self):
        c = self.clock()
        c.resume()
        self.time.advance(100)
        seen = c.now_ms()
        self.time.advance(7)
        self.assertEqual(c.freeze(seen), seen, "пауза замораживает ровно записанное в событии значение")
        self.assertEqual(c.now_ms(), seen)

    def test_misuse(self):
        c = self.clock()
        with self.assertRaises(ClockError):
            c.freeze()                      # уже стоят
        c.resume()
        with self.assertRaises(ClockError):
            c.resume()                      # уже идут
        with self.assertRaises(ClockError):
            c.freeze(5)                     # из будущего
        self.time.advance(10)
        with self.assertRaises(ClockError):
            c.freeze(-1)                    # раньше, чем часы запускались
        for bad in ({"initial_ms": -1}, {"time_scale": 0.5}, {"heartbeat_s": 0}):
            with self.assertRaises(ClockError):
                LifeClock(bad.pop("initial_ms", 0), **bad)

    def test_monotonic_ms_is_real_time(self):
        c = self.clock()
        before = c.monotonic_ms()
        self.time.advance_mono(1500)
        self.assertEqual(c.monotonic_ms() - before, 1500)
        self.assertEqual(c.calendar_now(), "2026-10-11T10:00:00.000Z")

    def test_heartbeat_and_recovery(self):
        store = Store.create(self.db, PID)
        self.addCleanup(store.close)
        c = self.clock(heartbeat_s=5)
        self.assertFalse(c.heartbeat(store), "пока часы стоят, писать нечего")
        c.resume()
        self.time.advance(4000)
        self.assertFalse(c.heartbeat_due())
        self.assertFalse(c.heartbeat(store))
        self.time.advance(1500)
        self.assertTrue(c.heartbeat_due())
        self.assertTrue(c.heartbeat(store))
        self.assertEqual(store.clock()[0], 5500)
        self.assertFalse(c.heartbeat_due(), "после записи отсчёт интервала начинается заново")
        self.time.advance(100)
        self.assertTrue(c.heartbeat(store, force=True))
        self.assertEqual(LifeClock.recovered_ms(store), 5600)
        again = LifeClock.recover(store, monotonic=self.time.mono, calendar=self.time.calendar)
        self.assertEqual(again.now_ms(), 5600)
        self.assertFalse(again.running)

    def test_recovered_ms_takes_max_of_heartbeat_and_last_event(self):
        store = Store.create(self.db, PID)
        self.addCleanup(store.close)
        self.assertEqual(LifeClock.recovered_ms(store), 0)
        store.append_event(plain_event(900))
        store.heartbeat(300, "2026-10-11T10:00:00.000Z")   # heartbeat старее события: берётся событие
        self.assertEqual(LifeClock.recovered_ms(store), 900)
        store.heartbeat(1200, "2026-10-11T10:00:01.000Z")
        self.assertEqual(LifeClock.recovered_ms(store), 1200)


class CalendarTest(unittest.TestCase):
    def test_helpers(self):
        self.assertRegex(calendar_now(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        a, b = "2026-10-11T10:00:00.000Z", "2026-10-11T10:00:01.250Z"
        self.assertEqual(calendar_diff_ms(b, a), 1250)
        self.assertEqual(calendar_diff_ms(a, b), -1250)
        self.assertEqual(parse_calendar(a).year, 2026)


class MomentsTest(unittest.TestCase):
    A, B, C = "mom_AAAAAAAA", "mom_BBBBBBBB", "mom_CCCCCCCC"

    def test_record_validation(self):
        good = life_moment(self.A, 100, "int_OWNER001")
        self.assertEqual(moment_errors(good), [])
        self.assertEqual(moment_errors(calendar_moment(self.B, "2026-10-12T09:00:00.000Z", "x")), [])
        self.assertTrue(moment_errors("не объект"))
        self.assertTrue(moment_errors({**good, "moment_id": "bad"}))
        self.assertTrue(moment_errors({**good, "owner_ref": ""}))
        self.assertTrue(moment_errors({**good, "fired_count": 1}))
        self.assertTrue(moment_errors({**good, "due_life_ms": -1}))
        self.assertTrue(moment_errors({**good, "due_life_ms": True}))
        self.assertTrue(moment_errors({**good, "extra": 1}))
        self.assertTrue(moment_errors(calendar_moment(self.B, "вчера", "x")))
        self.assertTrue(moment_errors({**calendar_moment(self.B, "2026-10-12T09:00:00.000Z", "x"), "due_life_ms": 1}))
        self.assertTrue(moment_errors({**good, "kind": "other"}))

    def test_due_order_and_kinds(self):
        ms = {m["moment_id"]: m for m in (
            life_moment(self.A, 500, "o"), life_moment(self.B, 100, "o"),
            calendar_moment(self.C, "2026-10-11T10:00:00.000Z", "o"),
            life_moment("mom_DDDDDDDD", 9000, "o"))}
        due = due_moments(ms, 500, "2026-10-11T10:00:05.000Z")
        self.assertEqual([d.moment["moment_id"] for d in due], [self.B, self.A, self.C])
        self.assertEqual([d.overdue_ms for d in due], [400, 0, 5000])
        self.assertTrue(all(d.n == 1 for d in due))
        self.assertEqual(due_moments(ms, 99, "2026-10-11T09:59:59.000Z"), [], "ничего не наступило")


if __name__ == "__main__":
    unittest.main()
