"""Сеанс, пауза, перезапуск, плановые моменты, вмешательства (docs/spec/time.md#sessions, #moments)."""
import unittest

from iam.clock import LifeClock, calendar_moment, life_moment
from iam.domain import Op
from iam.state import EventSpec, NotSupported, verify

from .helpers import StateCase, set_

MOM_A, MOM_B, MOM_C = "mom_AAAAAAAA", "mom_BBBBBBBB", "mom_CCCCCCCC"


class SessionTest(StateCase):
    def test_start_runs_the_clock(self):
        c = self.controller()
        self.assertEqual((c.session_state, c.life_time_ms), ("stopped", 0))
        self.time.advance(5000)
        self.assertEqual(c.life_time_ms, 0, "до сеанса время жизни не идёт")
        res = c.start_session()
        self.assertTrue(res.applied)
        self.assertEqual((c.session_state, c.clock.running), ("running", True))
        self.time.advance(1500)
        self.assertEqual(c.life_time_ms, 1500)
        ev = res.events[0]
        self.assertEqual((ev.type, ev.idempotency_key), ("session.started", f"ses:{c.session_id}:start"))
        self.assertEqual(ev.payload["session_id"], c.session_id)

    def test_wrong_session_state_is_refused_and_logged(self):
        c = self.controller()
        for call, name in ((c.pause_session, "pause"), (c.resume_session, "resume"), (c.stop_session, "stop")):
            res = call()
            self.assertEqual((res.status, res.reason), ("rejected", "bad_session_state"), name)
        c.start_session()
        self.assertEqual(c.start_session().reason, "bad_session_state", "второй сеанс одновременно не запускается")
        self.assertEqual(c.resume_session().reason, "bad_session_state")
        self.assertEqual(self.types(c).count("system.error"), 5)

    def test_session_id_is_never_reused(self):
        c = self.controller(running=True)
        sid = c.session_id
        c.stop_session()
        res = c.start_session(session_id=sid)
        self.assertEqual((res.status, res.reason), ("rejected", "session_id_used"))
        self.assertTrue(c.start_session(session_id="ses_BrandNewOne1").applied)

    def test_start_before_persona_created(self):
        c = self.controller(created=False)
        self.assertEqual(c.start_session().reason, "persona_not_created")
        self.assertFalse(c.clock.running)

    def test_pause_reasons_and_actors(self):
        c = self.controller(running=True)
        with self.assertRaises(ValueError):
            c.pause_session("because")
        res = c.pause_session("session_limit")
        self.assertEqual((res.applied, res.events[0].source.kind), (True, "core"))
        self.assertEqual(c.state["world_time"]["pause_reason"], "session_limit")
        c.resume_session()
        self.assertEqual(c.pause_session("operator").events[0].source.kind, "operator_panel")

    def test_pause_freezes_life_time_and_calendar_gap_is_data_only(self):
        """R-013/R-034: календарное время идёт при паузе, жизненное — нет."""
        c = self.controller(running=True)
        self.time.advance(600_000)
        paused = c.pause_session()
        self.assertEqual(c.life_time_ms, 600_000)
        self.assertEqual(paused.events[0].life_time_ms, 600_000)
        self.time.advance(86_400_000)
        self.assertEqual(c.life_time_ms, 600_000, "сутки паузы в жизни не засчитаны")
        arrived = c.record_event(EventSpec("input.arrived", "rx:tg:1", {"text": "Написал, пока ты на паузе."}))
        self.assertEqual((arrived.status, arrived.event.life_time_ms), ("recorded", 600_000))
        resumed = c.resume_session()
        self.assertEqual(resumed.events[0].payload["paused_calendar_ms"], 86_400_000)
        self.assertEqual(c.state["world_time"]["life_time_ms"], 600_000, "после возобновления — прежняя отметка")
        self.time.advance(300_000)
        self.assertEqual(c.life_time_ms, 900_000)
        self.assertEqual(verify(c.store), [])

    def test_calendar_jump_during_pause_never_gives_negative_gap(self):
        c = self.controller(running=True)
        c.pause_session()
        self.time.jump_calendar(-3_600_000)       # часы компьютера перевели назад
        res = c.resume_session()
        self.assertEqual(res.events[0].payload["paused_calendar_ms"], 0)
        self.assertEqual(c.life_time_ms, 0)

    def test_world_and_intentions_survive_pause(self):
        c = self.controller(running=True)
        c.apply(self.prop(c, set_("world_time", "world.desk_note", "не перечитана"),
                          set_("intentions", "int_FINISH001", {"text": "перечитать", "status": "active"})))
        before = c.state
        c.pause_session()
        self.time.advance(86_400_000)
        c.resume_session()
        after = c.state
        self.assertEqual(after["world_time"]["world"], before["world_time"]["world"])
        self.assertEqual(after["intentions"], before["intentions"])

    def test_nothing_changes_the_persona_during_pause(self):
        c = self.controller(running=True)
        c.pause_session()
        v = c.state_version
        self.assertEqual(c.apply(self.prop(c, set_("traits", "x", 1))).reason, "session_forbids")
        self.assertEqual(c.record_event(EventSpec("memory.retrieved", "rtv:cyc_ABCDEFGH:1")).reason, "session_forbids")
        self.assertEqual(c.state_version, v)
        self.assertNotIn("proposal.requested", self.types(c))

    def test_stop_from_running_and_from_paused(self):
        c = self.controller(running=True)
        self.time.advance(100)
        res = c.stop_session()
        self.assertEqual((res.applied, c.session_state, c.session_id, c.clock.running), (True, "stopped", None, False))
        self.assertEqual(c.life_time_ms, 100)
        self.assertTrue(c.start_session().applied, "после остановки можно начать новый сеанс")
        c.pause_session()
        self.time.advance(50)
        self.assertTrue(c.stop_session().applied)
        self.assertEqual(c.life_time_ms, 100)

    def test_each_session_restarts_counters(self):
        c = self.controller(running=True)
        c.pause_session()
        c.resume_session()
        c.stop_session()
        c.start_session()
        self.assertEqual((c.state["world_time"]["pause_count"], c.state["world_time"]["resume_count"]), (0, 0))
        self.assertTrue(c.pause_session().applied)


class FailedStepTest(StateCase):
    """Если переход сеанса не прошёл проверку, часы остаются в прежнем состоянии: время не замирает и не идёт зря."""

    def deny_session(self, c, target):
        c.add_validator("deny", lambda ch: ["запрещено"] if ch.after["world_time"]["session_state"] == target else [])

    def test_refused_pause_keeps_clock_running(self):
        c = self.controller(running=True)
        self.deny_session(c, "paused")
        self.assertEqual(c.pause_session().reason, "validator:deny")
        self.assertEqual((c.session_state, c.clock.running), ("running", True))

    def test_refused_resume_keeps_clock_stopped(self):
        c = self.controller(running=True)
        c.pause_session()
        self.deny_session(c, "running")
        self.assertEqual(c.resume_session().reason, "validator:deny")
        self.assertEqual((c.session_state, c.clock.running), ("paused", False))

    def test_refused_stop_keeps_session(self):
        c = self.controller(running=True)
        self.deny_session(c, "stopped")
        self.assertEqual(c.stop_session().reason, "validator:deny")
        self.assertEqual((c.session_state, c.clock.running), ("running", True))


class CrashTest(StateCase):
    def test_crash_recovery_keeps_life_time_and_records_pause(self):
        c = self.controller(running=True)
        c.apply(self.prop(c, set_("world_time", "world.desk_note", "не перечитана")))
        self.time.advance(300_000)
        c.record_event(EventSpec("input.arrived", "rx:tg:1"))        # последнее событие: жизнь 300000
        self.time.advance(2_000)
        c.clock.heartbeat(c.store, force=True)                       # контрольная точка: 302000
        self.time.advance(7_000)                                     # прошло после неё — аварийно не засчитывается
        sid, world = c.session_id, c.state["world_time"]["world"]
        self.time.advance(3_600_000)                                 # час процесс не работал
        c2 = self.reopen(c)
        self.assertEqual(c2.session_state, "paused")
        self.assertEqual(c2.state["world_time"]["pause_reason"], "crash")
        self.assertEqual(c2.life_time_ms, 302_000)
        crash = [e for e in c2.store.events() if e.type == "session.paused"][-1]
        self.assertEqual((crash.payload["reason"], crash.life_time_ms, crash.source.kind), ("crash", 302_000, "core"))
        self.assertEqual(c2.state["world_time"]["world"], world, "мир после аварии — последний зафиксированный")
        resumed = c2.resume_session()
        gap = resumed.events[0].payload
        self.assertEqual((gap["was_paused_for"], gap["session_id"]), ("crash", sid))
        self.assertEqual(gap["paused_calendar_ms"], 3_600_000 + 7_000, "простой считается календарным, не жизненным")
        self.time.advance(1000)
        self.assertEqual(c2.life_time_ms, 303_000)
        self.assertEqual(verify(c2.store), [])

    def test_recovery_uses_last_event_when_it_is_later_than_heartbeat(self):
        c = self.controller(running=True)
        self.time.advance(1000)
        c.clock.heartbeat(c.store, force=True)                       # 1000
        self.time.advance(4000)
        c.record_event(EventSpec("input.arrived", "rx:tg:1"))        # 5000
        self.assertEqual(self.reopen(c).life_time_ms, 5000)

    def test_clean_restart_after_pause_is_not_a_crash(self):
        c = self.controller(running=True)
        self.time.advance(500)
        c.pause_session()
        c2 = self.reopen(c)
        self.assertEqual([e.payload["reason"] for e in c2.store.events() if e.type == "session.paused"], ["operator"])
        self.assertEqual(c2.life_time_ms, 500)
        c3 = self.reopen(c2, recover=False)
        self.assertEqual(c3.session_state, "paused")

    def test_recover_can_be_switched_off(self):
        c = self.controller(running=True)
        c2 = self.reopen(c, recover=False)
        self.assertEqual(c2.session_state, "running", "состояние показано как есть; решает вызывающий")


class MomentsTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)

    def test_life_moment_fires_only_when_life_time_reaches_it(self):
        c = self.c
        res = c.schedule_moment(life_moment(MOM_A, 60_000, "int_OWNER001"))
        self.assertTrue(res.applied, res.violations)
        self.assertIn(MOM_A, c.state["world_time"]["moments"])
        self.time.advance(59_999)
        self.assertEqual(c.fire_due_moments(), [])
        self.time.advance(1)
        fired = c.fire_due_moments()
        self.assertEqual([r.status for r in fired], ["applied"])
        ev = fired[0].events[0]
        self.assertEqual((ev.type, ev.idempotency_key, ev.payload["overdue_ms"], ev.payload["owner_ref"]),
                         ("time.moment_due", f"due:{MOM_A}:1", 0, "int_OWNER001"))
        self.assertNotIn(MOM_A, c.state["world_time"]["moments"])
        self.assertEqual(c.fire_due_moments(), [], "одноразовый момент второй раз не наступает")

    def test_life_moment_does_not_fire_or_accumulate_in_pause(self):
        c = self.c
        c.schedule_moment(life_moment(MOM_A, 1000, "o"))
        self.time.advance(400)
        c.pause_session()
        self.time.advance(10 * 86_400_000)
        self.assertEqual(c.tick(), [], "на паузе моменты не наступают")
        c.resume_session()
        self.assertEqual(c.fire_due_moments(), [], "осталось ещё 600 мс жизни")
        self.time.advance(600)
        self.assertEqual(len(c.tick()), 1)

    def test_calendar_moment_overdue_in_pause_fires_once_after_resume(self):
        c = self.c
        due = "2026-10-11T12:00:00.000Z"
        c.schedule_moment(calendar_moment(MOM_B, due, "agreement"))
        c.pause_session()
        self.time.advance(5 * 3_600_000)     # срок прошёл в паузе (теперь 15:00)
        self.assertEqual(c.fire_due_moments(), [])
        c.resume_session()
        first = c.fire_due_moments()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].events[0].payload["overdue_ms"], 3 * 3_600_000)
        self.assertEqual(first[0].events[0].payload["due_calendar"], due)
        self.assertEqual(c.fire_due_moments(), [])

    def test_several_due_moments_fire_in_order_each_as_own_transition(self):
        c = self.c
        for m in (life_moment(MOM_A, 20, "a"), life_moment(MOM_B, 10, "b"), calendar_moment(MOM_C, "2026-10-11T10:00:00.000Z", "c")):
            c.schedule_moment(m)
        self.time.advance(30)
        v = c.state_version
        fired = c.fire_due_moments()
        self.assertEqual([r.events[0].payload["moment_id"] for r in fired], [MOM_B, MOM_A, MOM_C])
        self.assertEqual(c.state_version, v + 3)
        self.assertEqual(c.state["world_time"]["moments"], {})
        self.assertEqual(verify(c.store), [])

    def test_moment_id_cannot_be_reused_after_firing(self):
        c = self.c
        c.schedule_moment(life_moment(MOM_A, 10, "o"))
        self.time.advance(10)
        c.fire_due_moments()
        self.assertEqual(c.schedule_moment(life_moment(MOM_A, 500, "o")).reason, "moment_exists")
        self.time.advance(1000)
        self.assertEqual(c.fire_due_moments(), [])

    def test_schedule_validation_and_cancel(self):
        c = self.c
        self.assertEqual(c.schedule_moment({"moment_id": "bad"}).reason, "bad_moment")
        c.schedule_moment(life_moment(MOM_A, 100, "o"))
        self.assertEqual(c.schedule_moment(life_moment(MOM_A, 200, "o")).reason, "moment_exists")
        self.assertTrue(c.cancel_moment(MOM_A).applied)
        self.assertEqual(c.cancel_moment(MOM_A).reason, "path_missing")
        self.time.advance(1000)
        self.assertEqual(c.fire_due_moments(), [])

    def test_cannot_schedule_while_paused_or_through_apply(self):
        c = self.c
        self.assertEqual(c.apply(self.prop(c, set_("world_time", f"moments.{MOM_A}", life_moment(MOM_A, 1, "o")),
                                           actor="operator")).reason, "protected_field")
        c.pause_session()
        self.assertEqual(c.schedule_moment(life_moment(MOM_A, 1, "o")).reason, "session_forbids")

    def test_tick_writes_heartbeat(self):
        c = self.c
        self.time.advance(6000)
        self.assertEqual(c.tick(), [])
        self.assertEqual(c.store.clock()[0], 6000)


class InterventionTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)

    def test_technical_fix_works_in_pause_and_is_a_visible_event(self):
        c = self.c
        c.pause_session()
        res = c.intervene("technical_fix", "исправить опечатку в черте", [set_("traits", "curious", {"value": 0.6})])
        self.assertTrue(res.applied, res.violations)
        ev = res.events[0]
        self.assertEqual((ev.type, ev.source.kind), ("operator.intervention", "operator_panel"))
        self.assertEqual((ev.payload["kind"], ev.payload["operator_channel"], ev.payload["visible_to_persona"]),
                         ("technical_fix", "panel", True))
        self.assertEqual(c.state["traits"]["curious"]["value"], 0.6)
        self.assertEqual(c.session_state, "paused", "вмешательство сеанс не меняет")

    def test_reason_is_required(self):
        c = self.c
        for reason in ("", "  ", "ab"):
            res = c.intervene("technical_fix", reason, [set_("traits", "x", 1)])
            self.assertEqual(res.reason, "invalid_transition", repr(reason))
        self.assertNotIn("x", c.state["traits"])

    def test_cannot_touch_session_fields_or_use_other_kinds(self):
        c = self.c
        self.assertEqual(c.intervene("technical_fix", "подправить время", [set_("world_time", "life_time_ms", 5)]).reason,
                         "protected_field")
        with self.assertRaises(NotSupported):
            c.intervene("grant_capability", "дать право", [])

    def test_intervention_does_not_rewrite_journal(self):
        c = self.c
        n = c.store.last_seq()
        c.intervene("technical_fix", "поправить", [Op("traits", "curious", "remove")])
        self.assertEqual(c.store.last_seq(), n + 1, "исправление — новое событие")


class ClockWiringTest(StateCase):
    def test_default_clock_is_recovered_from_store(self):
        c = self.controller(running=True)
        self.time.advance(250)
        c.pause_session()
        c.store.close()
        from iam.storage import Store
        from iam.state import StateController
        store = Store.open(self.db)
        self.addCleanup(store.close)
        c2 = StateController(store)       # часы по умолчанию: восстановлены из хранилища
        self.assertEqual((c2.life_time_ms, c2.clock.running), (250, False))
        self.assertIsInstance(c2.clock, LifeClock)


if __name__ == "__main__":
    unittest.main()
