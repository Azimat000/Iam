"""Запуск по событиям (R-036): нет событий — нет обращений к провайдеру; входящее не читается без решения (P-1);
предел решений; пауза."""
import unittest
from dataclasses import replace

from iam.cycle import TRIGGERS, input_id_for
from iam.domain import MemoryRecord, Source
from iam.interfaces import ProviderError
from iam.stubs import Reply, StubMemory, act, rest

from state.helpers import PID

from .helpers import CAP_NOTES, INTENTION, CycleCase

SECRET = "СЕКРЕТНЫЙ-ТЕКСТ-НЕПРОЧИТАННОГО"


class NoEventsTest(CycleCase):
    def test_without_events_the_provider_is_never_called(self):
        self.build(Reply((rest(),)))
        for _ in range(5):
            self.assertEqual(self.runner.run_pending(), [])
        self.assertEqual(self.provider.calls, [])
        self.time.advance(3_600_000)                      # час тишины
        self.assertEqual(self.runner.run_pending(), [])
        self.assertEqual(self.provider.calls, [])
        self.assertNotIn("proposal.requested", self.types())
        self.assertNotIn("attention.chosen", self.types())

    def test_planned_moment_starts_exactly_one_cycle(self):
        """Сценарий «Отсутствие новых сообщений»: выбор занятия вызван плановым моментом, не опросом."""
        from iam.clock import life_moment
        self.build(Reply((act(CAP_NOTES, {"title": "глава 4"}),)))
        self.c.schedule_moment(life_moment("mom_Quiet00001", self.c.life_time_ms + 20 * 60_000, INTENTION))
        self.assertEqual(self.runner.run_pending(), [])
        self.time.advance(3_600_000)
        (report,) = self.runner.run_pending()
        self.assertEqual(report.outcome, "done")
        trigger = self.c.find_event(event_id=report.trigger)
        self.assertEqual(trigger.type, "time.moment_due")
        chosen = self.c.find_event(key=f"att:{report.cycle_id}")
        self.assertIn(trigger.event_id, chosen.payload["trigger"])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.runner.run_pending(), [])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertNotIn("input.read", self.types())
        self.assertNotIn("system.error", self.types())
        self.assertEqual(self.c.state["intentions"][INTENTION]["status"], "active")
        self.assertIn("дочитать главу 4", self.provider.calls[0]["context"][0]["text"])

    def test_cycle_is_idempotent_for_one_trigger(self):
        cycle = self.build(Reply((rest(),)), Reply((rest(),)))
        trigger = self.due()
        self.assertEqual(cycle.run(trigger).outcome, "done")
        again = cycle.run(trigger)
        self.assertEqual((again.outcome, again.reason), ("skipped", "already_done"))
        self.assertEqual(len(self.provider.calls), 1)

    def test_no_cycle_while_paused_and_it_waits_for_resume(self):
        cycle = self.build(Reply((rest(),)))
        self.assertTrue(self.c.pause_session().applied)
        paused = cycle.run(self.c.store.get_event(1))
        self.assertEqual((paused.outcome, paused.reason), ("skipped", "session_not_running"))
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.runner.run_pending(), [])
        self.assertTrue(self.c.resume_session().applied)
        self.assertEqual(self.provider.calls, [])


class IngestTest(CycleCase):
    def test_unread_message_content_never_reaches_the_model(self):
        self.build(Reply((rest("жду"),)))
        self.creator.say(SECRET, "tg-1")
        (report,) = self.runner.run_pending()
        self.assertEqual(report.outcome, "done")
        arrived = self.c.find_event(key="rx:stub:tg-1")
        self.assertEqual(arrived.type, "input.arrived")
        self.assertFalse(arrived.changes_state)
        self.assertEqual(arrived.payload["input_id"], input_id_for("stub", "tg-1"))
        (request,) = self.provider.calls
        self.assertNotIn(SECRET, str(request))
        first = request["context"][0]
        self.assertEqual((first["layer"], first["ref"]), ("notice", arrived.event_id))
        self.assertNotIn("input.read", self.types())
        self.assertEqual(self.c.state["affect"]["attention"]["focus"], "rest")   # ответ модели применён после внимания

    def test_repeated_delivery_creates_one_event_and_one_cycle(self):
        self.build(Reply((rest(),)), Reply((rest(),)))
        self.creator.say("привет", "tg-9")
        self.creator.say("привет", "tg-9")
        self.assertEqual(len(self.runner.run_pending()), 1)
        self.assertEqual(self.types().count("input.arrived"), 1)

    def test_messages_arrive_in_pause_without_calling_the_model(self):
        self.build(Reply((rest(),)))
        self.c.pause_session()
        self.creator.say("пока тебя нет", "tg-2")
        self.assertEqual(self.runner.run_pending(), [])
        self.assertIn("input.arrived", self.types())
        self.assertEqual(self.provider.calls, [])
        self.c.resume_session()
        (report,) = self.runner.run_pending()
        self.assertEqual(report.outcome, "done")

    def test_archive_layer_is_not_offered_to_the_model(self):
        def rec(mid, layer):
            return MemoryRecord(memory_id=mid, persona_id=PID, layer=layer, kind="semantic",
                                origin="observation", content={"text": f"запись {layer}"}, source=Source("core", "t"),
                                created_at="2026-10-11T10:00:00.000Z", created_life_ms=0, sources=("evt_ABCDEFGH",))

        class Leaky(StubMemory):
            def retrieve(self, query, *, limit=5):
                return self.records   # «ошибочно» отдаёт и архив
        cycle = self.build(Reply((rest(),)), memory=Leaky([rec("mem_Archive001", "archive"), rec("mem_Personal01", "personal")]))
        cycle.run(self.due())
        refs = [i["ref"] for i in self.provider.calls[0]["context"]]
        self.assertIn("mem_Personal01", refs)
        self.assertNotIn("mem_Archive001", refs)
        retrieved = next(e for e in self.c.store.events() if e.type == "memory.retrieved")
        self.assertEqual(retrieved.payload["memory_ids"], ["mem_Personal01"])


class LimitTest(CycleCase):
    def test_decision_limit_per_pass_and_the_rest_waits(self):
        self.build(*[Reply((rest(),)) for _ in range(3)])
        self.runner.max_cycles = 2
        triggers = [self.due() for _ in range(3)]
        first = self.runner.run_pending()
        self.assertEqual([r.trigger for r in first], [t.event_id for t in triggers[:2]])
        second = self.runner.run_pending()
        self.assertEqual([r.trigger for r in second], [triggers[2].event_id])
        self.assertEqual(self.runner.run_pending(), [])
        self.assertEqual(len(self.provider.calls), 3)

    def test_unlisted_events_are_not_triggers(self):
        self.assertEqual(set(TRIGGERS), {"time.moment_due", "input.arrived", "activity.completed"})
        self.build(Reply((rest(),)))
        self.runner.triggers = ("activity.completed",)
        self.due()
        self.assertEqual(self.runner.run_pending(), [])
        self.assertEqual(self.provider.calls, [])

    def test_activity_completed_trigger_uses_generic_context(self):
        cycle = self.build(Reply((rest(),)))
        report = cycle.run(replace(self.c.store.get_event(1), type="activity.completed"))
        self.assertEqual(report.outcome, "done")
        self.assertEqual(self.provider.calls[0]["context"][0]["text"], "Событие activity.completed.")


class RobustnessTest(CycleCase):
    def test_provider_error_keeps_the_trigger_for_a_later_round(self):
        self.build(Reply(error=ProviderError("лимит")), Reply((rest(),)))
        self.due()
        first = self.runner.run_pending()
        self.assertEqual([(r.outcome, r.reason) for r in first], [("no_decision", "provider_error")])
        second = self.runner.run_pending()
        self.assertEqual([r.outcome for r in second], ["done"])
        self.assertEqual(self.runner.run_pending(), [])

    def test_endless_failures_give_up_after_max_rounds(self):
        self.build(*[Reply(error=ProviderError("лимит")) for _ in range(10)])
        self.due()
        passes = [self.runner.run_pending() for _ in range(6)]
        self.assertEqual([len(p) for p in passes], [1, 1, 1, 0, 0, 0])
        self.assertEqual(len(self.provider.calls), 3)
        gave_up = [e for e in self.c.store.events() if e.type == "system.error" and e.payload["fault"] == "cycle_gave_up"]
        self.assertEqual(len(gave_up), 1)

    def test_exception_in_one_cycle_does_not_stop_the_others_and_is_not_retried(self):
        class Broken(StubMemory):
            calls = 0

            def retrieve(self, query, *, limit=5):
                Broken.calls += 1
                if Broken.calls == 1:
                    raise RuntimeError("память упала")
                return []
        self.build(Reply((rest(),)), memory=Broken())
        first, second = self.due(), self.due()
        reports = self.runner.run_pending()
        self.assertEqual([r.trigger for r in reports], [second.event_id])
        crash = [e for e in self.c.store.events() if e.type == "system.error" and e.payload["fault"] == "cycle_crash"]
        self.assertEqual([e.payload["trigger"] for e in crash], [first.event_id])
        self.assertEqual(self.runner.run_pending(), [])
        self.assertEqual(len(self.provider.calls), 1)

    def test_skipped_old_triggers_do_not_use_up_the_decision_limit(self):
        self.build(*[Reply((rest(),)) for _ in range(8)])
        old = [self.due() for _ in range(4)]
        for e in old:                      # эти поводы уже были обработаны раньше
            self.assertEqual(self.cycle.run(e).outcome, "done")
        fresh = self.due()
        self.runner.max_cycles = 1
        reports = self.runner.run_pending()
        self.assertEqual([r.trigger for r in reports], [fresh.event_id])


if __name__ == "__main__":
    unittest.main()
