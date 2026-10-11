"""Модель только предлагает (R-104, R-117); выполнение не объявляется без подтверждения (R-184, R-199, R-222);
сценарии «Сбой во время действия» и «Нет нужного инструмента»; просроченный ответ не применяется."""
import unittest

from iam.cycle import DEFAULT_ALLOWED
from iam.interfaces import Capability, ProviderError
from iam.stubs import (Reply, ScriptExhausted, StubCapabilities, StubCreatorBridge, StubWorld, WorldCrash, act, ask,
                       attend, change, rest, say)

from .helpers import CAP_NOTES, CAP_PLOT, CAP_TG, INTENTION, CycleCase, set_


def status_of(c, action_id):
    return c.state["world_time"]["world"]["actions"][action_id]["status"]


class ToolUseTest(CycleCase):
    """R-104/R-117: признаки инструментов или готового «выполнено» в предложении — оно не применяется."""

    def run_with(self, proposal, **kw):
        cycle = self.build(Reply((proposal,), **kw))
        before = self.c.state
        report = cycle.run(self.due())
        return report, before

    def assertNothingHappened(self, report, before):
        self.assertEqual(report.outcome, "rejected", report)
        self.assertEqual(self.world.executed, [])
        self.assertNotIn("action.attempted", self.types())
        self.assertNotIn("action.confirmed", self.types())
        self.assertNotIn("memory.recorded", self.types())
        self.assertEqual(self.c.state["world_time"]["world"], before["world_time"]["world"])

    def test_tool_calls_inside_action_params_reject_the_whole_response(self):
        good, bad = act(CAP_NOTES, {"title": "ok"}), act(CAP_NOTES, {"x": {"tool_calls": [{"name": "bash"}]}})
        cycle = self.build(Reply((good, bad)))
        report = cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("rejected", "tool_use_detected"))
        self.assertEqual(self.world.executed, [])
        self.assertEqual(self.types().count("proposal.rejected"), 2)       # отклонены оба: ответ непригоден (R-117)
        self.assertIn("system.error", self.types())
        self.assertNotIn("proposal.accepted", self.types())

    def test_claim_of_execution_in_body_is_rejected(self):
        p = act(CAP_NOTES, {"title": "x"})
        p["body"]["executed"] = True
        report, before = self.run_with(p)
        self.assertNothingHappened(report, before)
        rej = [e for e in self.c.store.events() if e.type == "proposal.rejected"]
        self.assertEqual(rej[0].payload["reason"], "tool_use_detected")

    def test_claimed_result_on_a_state_change_is_flagged_too(self):
        p = change([{"group": "preferences", "path": "likes", "op": "set", "value": 1}])
        p["body"]["stdout"] = "готово"
        report, before = self.run_with(p)
        self.assertEqual(report.reason, "tool_use_detected")
        self.assertEqual(self.c.state["preferences"], before["preferences"])

    def test_tool_use_flag_from_provider_rejects_everything(self):
        report, before = self.run_with(act(CAP_NOTES), tool_use=True)
        self.assertNothingHappened(report, before)

    def test_ops_values_are_data_not_signs(self):
        """В state_change значения операций — данные состояния: ключ «result» внутри значения не признак."""
        p = change([{"group": "preferences", "path": "notes", "op": "set", "value": {"result": "любимое"}}])
        cycle = self.build(Reply((p,)), allowed_proposals=(*DEFAULT_ALLOWED, "state_change"))
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "done")
        self.assertEqual(self.c.state["preferences"]["notes"], {"result": "любимое"})

    def test_unknown_body_field_rejects_only_that_proposal(self):
        odd = rest("отдых")
        odd["body"]["whatever"] = 1
        cycle = self.build(Reply((odd, attend("rest"))))
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "done")
        rej = [e for e in self.c.store.events() if e.type == "proposal.rejected"]
        self.assertEqual([e.payload["reason"] for e in rej], ["unknown_body_field"])
        self.assertEqual(self.c.state["affect"]["attention"]["focus"], "rest")

    def test_model_cannot_confirm_actions_directly(self):
        """Ни ответ модели, ни её state_change не пишут исход действия: статус меняет только автомат."""
        cycle = self.build(Reply((act(CAP_NOTES),)), allowed_proposals=(*DEFAULT_ALLOWED, "state_change"))
        report = cycle.run(self.due())
        aid = report.actions[0].action_id
        for value, path in (("failed", f"world.actions.{aid}.status"), ("confirmed", f"world.actions.{aid}")):
            forged = change([{"group": "world_time", "path": path, "op": "set", "value": value}])
            self.provider.queue(Reply((forged,)))
            again = cycle.run(self.due())
            self.assertEqual(again.outcome, "rejected", again)
        rej = [e.payload["reason"] for e in self.c.store.events() if e.type == "proposal.rejected"]
        self.assertEqual(rej, ["reserved_path", "reserved_path"])
        self.assertEqual(status_of(self.c, aid), "confirmed")


    def test_model_cannot_build_the_outcome_by_a_chain_of_state_changes(self):
        """Три ответа подряд (accepted → attempted → confirmed) со ссылками на «нужные» события исход не дают."""
        cycle = self.build(Reply((act(CAP_NOTES),)), allowed_proposals=(*DEFAULT_ALLOWED, "state_change"))
        report = cycle.run(self.due())
        accepted = next(e for e in self.c.store.events() if e.type == "proposal.accepted")
        attempted = next(e for e in self.c.store.events() if e.type == "action.attempted")
        fake = "act_Forged00001"
        steps = [{"group": "world_time", "path": f"world.actions.{fake}", "op": "set", "value": {"status": "accepted"}},
                 {"group": "world_time", "path": f"world.actions.{fake}.status", "op": "transition", "value": "attempted"},
                 {"group": "world_time", "path": f"world.actions.{fake}.status", "op": "transition", "value": "confirmed"}]
        for step, cite in zip(steps, (accepted, accepted, attempted)):
            p = change([step], cites=[cite.event_id])
            self.provider.queue(Reply((p,)))
            self.assertEqual(cycle.run(self.due()).outcome, "rejected")
        self.assertNotIn(fake, self.c.state["world_time"]["world"]["actions"])
        self.assertEqual(len(self.world.executed), 1)
        self.assertEqual(report.actions[0].status, "confirmed")


class NoToolTest(CycleCase):
    """Сценарий «Нет нужного инструмента» (scenarios/required/no_tool.toml): нет выдуманного выполнения."""

    def test_claim_without_capability_is_rejected_and_wish_is_kept(self):
        claim = act(CAP_PLOT, {"table": "data"}, rationale="Я построила график по таблице и сохранила его.")
        cycle = self.build(Reply((claim,)))
        report = cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("rejected", "no_proposal_accepted"))
        rej = [e for e in self.c.store.events() if e.type == "proposal.rejected"]
        self.assertEqual([e.payload["reason"] for e in rej], ["capability_not_available"])
        self.assertEqual(self.world.executed, [])
        self.assertNotIn("action.confirmed", self.types())
        self.assertEqual(self.memory.records, [])
        self.assertEqual(list(self.c.store.search_memory("построила")), [])
        self.assertEqual(self.c.state["intentions"][INTENTION]["status"], "active")

    def test_alternative_is_possible_after_refusal(self):
        """Не вышло с недоступным — в следующем ответе личность может обратиться к оператору (но не более одной)."""
        cycle = self.build(Reply((act(CAP_PLOT),)), Reply((ask("need_plotting", "Подключи возможность строить графики"),)))
        first = cycle.run(self.due())
        second = cycle.run(self.due())
        self.assertEqual((first.outcome, second.outcome), ("rejected", "done"))
        self.assertEqual(self.types().count("creator.request"), 1)
        self.assertEqual(len(self.creator.submitted), 1)
        self.assertEqual(self.c.state["intentions"][INTENTION]["status"], "active")

    def test_unavailable_capability_is_not_offered_and_not_accepted(self):
        self.build(Reply((act(CAP_NOTES),)))
        self.caps.set_status(CAP_NOTES, "needs_permission")
        report = self.cycle.run(self.due())
        self.assertNotIn(CAP_NOTES, self.provider.calls[0]["allowed_actions"])
        self.assertEqual(report.outcome, "rejected")
        self.assertEqual(self.world.executed, [])

    def test_capability_outside_allowed_actions_is_rejected(self):
        """Возможность появилась в реестре уже после запроса: в allowed_actions её не было (C-6)."""
        new = "cap_Library001"
        reply = Reply((act(new),), before=lambda req: self.caps.register(Capability(new, "библиотека")))
        cycle = self.build(reply)
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "rejected")
        rej = [e for e in self.c.store.events() if e.type == "proposal.rejected"]
        self.assertEqual(rej[0].payload["reason"], "capability_not_allowed")

    def test_kind_outside_allowed_proposals_is_rejected(self):
        cycle = self.build(Reply((act(CAP_NOTES),)), allowed_proposals=("no_action",))
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "rejected")
        rej = [e for e in self.c.store.events() if e.type == "proposal.rejected"]
        self.assertEqual(rej[0].payload["reason"], "kind_not_allowed")

    def test_two_actions_in_one_answer_exceed_the_limit(self):
        cycle = self.build(Reply((act(CAP_NOTES, {"n": 1}), say(CAP_TG, "operator", "привет"))))
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "done")
        self.assertEqual(len(report.actions), 1)
        self.assertEqual(len(self.world.executed), 1)
        rej = [e for e in self.c.store.events() if e.type == "proposal.rejected"]
        self.assertEqual([e.payload["reason"] for e in rej], ["too_many_actions"])

    def test_limit_can_be_raised(self):
        cycle = self.build(Reply((act(CAP_NOTES, {"n": 1}), say(CAP_TG, "operator", "привет"))), max_actions=2)
        report = cycle.run(self.due())
        self.assertEqual([a.status for a in report.actions], ["confirmed", "confirmed"])
        self.assertEqual(len(self.world.executed), 2)


class ActionFailureTest(CycleCase):
    """Сценарий «Сбой во время действия» (scenarios/required/action_crash.toml)."""

    def test_crash_inside_the_world_is_unknown_outcome_not_success(self):
        cycle = self.build(Reply((act(CAP_NOTES, {"title": "заметка"}),)), world=StubWorld().will(CAP_NOTES, "crash"))
        report = cycle.run(self.due())
        (action,) = report.actions
        self.assertEqual((report.outcome, action.status), ("done", "unknown_outcome"))
        types = self.types()
        self.assertIn("action.attempted", types)
        self.assertIn("action.unknown_outcome", types)
        self.assertIn("system.error", types)
        for absent in ("action.confirmed", "activity.completed", "memory.recorded"):
            self.assertNotIn(absent, types)
        self.assertEqual(status_of(self.c, action.action_id), "unknown_outcome")
        self.assertEqual(self.memory.records, [])
        self.assertEqual(list(self.c.store.search_memory("заметка")), [])

    def test_world_failure_is_recorded_as_failed_and_not_remembered_as_done(self):
        cycle = self.build(Reply((act(CAP_NOTES),)), world=StubWorld("fail"))
        report = cycle.run(self.due())
        self.assertEqual(report.actions[0].status, "failed")
        self.assertIn("action.failed", self.types())
        self.assertNotIn("action.confirmed", self.types())
        self.assertNotIn("memory.recorded", self.types())
        self.assertEqual([s.stage for s in report.stages][-1], "confirmation")

    def test_lost_ack_of_a_message_is_out_unknown_and_not_resent(self):
        cycle = self.build(Reply((say(CAP_TG, "operator", "Заметка готова."),)), world=StubWorld("unknown"))
        report = cycle.run(self.due())
        types = self.types()
        self.assertEqual(report.actions[0].status, "unknown_outcome")
        self.assertEqual(types.count("message.out_unknown"), 1)
        self.assertEqual(types.count("message.out_requested"), 1)
        self.assertNotIn("message.out_sent", types)
        self.assertEqual(len(self.world.executed), 1)
        # и повторный запуск того же цикла ничего не отправляет
        self.assertEqual(cycle.run(self.c.find_event(event_id=report.trigger)).reason, "already_done")
        self.assertEqual(len(self.world.executed), 1)

    def test_confirmed_message_is_out_sent_by_the_world(self):
        cycle = self.build(Reply((say(CAP_TG, "operator", "Привет"),)))
        report = cycle.run(self.due())
        sent = self.c.find_event(key=f"out:{report.actions[0].action_id}:ok")
        self.assertEqual((sent.type, sent.source.kind), ("message.out_sent", "world"))
        self.assertEqual(self.memory.records, [])   # запись идёт только через привратника
        self.assertTrue(report.actions[0].memory_id)

    def test_garbage_from_the_world_is_unknown(self):
        class Garbage:
            def execute(self, request):
                return "всё хорошо"
        cycle = self.build(Reply((act(CAP_NOTES),)), world=Garbage())
        report = cycle.run(self.due())
        self.assertEqual(report.actions[0].status, "unknown_outcome")
        self.assertNotIn("action.confirmed", self.types())

    def test_nothing_to_remember_skips_memory_update(self):
        class Forgetful:
            def retrieve(self, query, *, limit=5):
                return []

            def describe(self, action, outcome):
                return None
        cycle = self.build(Reply((act(CAP_NOTES),)), memory=Forgetful())
        report = cycle.run(self.due())
        self.assertEqual(report.stages[-1].stage, "memory_update")
        self.assertEqual(report.stages[-1].status, "skipped")
        self.assertEqual(report.actions[0].status, "confirmed")
        self.assertIsNone(report.actions[0].memory_id)

    def test_process_death_mid_action_then_restart_settles_as_unknown(self):
        class Killed(BaseException):
            """Не Exception: процесс умирает, цикл перехватить не может."""

        class Dying:
            def execute(self, request):
                raise Killed()
        cycle = self.build(Reply((act(CAP_NOTES, {"title": "заметка"}),)), world=Dying())
        trigger = self.due()
        with self.assertRaises(Killed):
            cycle.run(trigger)
        aid = next(iter(self.c.state["world_time"]["world"]["actions"]))
        self.assertEqual(status_of(self.c, aid), "attempted")
        # перезапуск: сеанс встал на паузу «crash», после возобновления зависшее действие получает «неизвестно»
        c2 = self.reopen(self.c)
        self.assertEqual(c2.session_state, "paused")
        self.assertTrue(c2.resume_session().applied)
        self.c = c2
        cycle2 = self.cycle.__class__(c2, self.provider, self.memory, StubWorld(), self.caps, self.creator)
        self.assertEqual(cycle2.settle_unfinished(), [aid])
        self.assertEqual(status_of(c2, aid), "unknown_outcome")
        types = self.types(c2)
        self.assertIn("action.unknown_outcome", types)
        for absent in ("action.confirmed", "activity.completed", "memory.recorded"):
            self.assertNotIn(absent, types)
        self.assertEqual(cycle2.settle_unfinished(), [])   # второй раз ничего не осталось

    def test_settle_unfinished_does_nothing_in_pause(self):
        cycle = self.build()
        self.c.pause_session()
        self.assertEqual(cycle.settle_unfinished(), [])


class StaleAnswerTest(CycleCase):
    """Просроченный ответ провайдера не применяется (через привратник S05); перезапрос — не более max_rerequest."""

    def move_state(self, request):
        self.assertTrue(self.c.apply(self.prop(self.c, set_("preferences", "mood_note", "пока модель думала"))).applied)

    def test_stale_answer_is_not_applied_and_a_fresh_one_is(self):
        cycle = self.build(Reply((act(CAP_NOTES, {"old": True}),), before=self.move_state), Reply((rest("ладно"),)))
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "done")
        self.assertEqual(len(report.requests), 2)
        self.assertEqual(self.world.executed, [])
        self.assertIn("proposal.stale", self.types())
        self.assertNotIn("action.attempted", self.types())
        first, second = self.provider.calls
        self.assertLess(first["state_version"], second["state_version"])

    def test_stale_without_budget_ends_without_decision_and_keeps_the_trigger(self):
        cycle = self.build(Reply((act(CAP_NOTES),), before=self.move_state), max_rerequest=0)
        report = cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("no_decision", "version"))
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.world.executed, [])

    def test_runner_returns_to_the_trigger_in_the_next_pass(self):
        self.build(Reply((act(CAP_NOTES),), before=self.move_state), Reply((rest("теперь можно"),)), max_rerequest=0)
        self.due()
        first = self.runner.run_pending()
        self.assertEqual([r.outcome for r in first], ["no_decision"])
        second = self.runner.run_pending()
        self.assertEqual([r.outcome for r in second], ["done"])
        self.assertNotEqual(first[0].cycle_id, second[0].cycle_id)
        self.assertEqual(self.runner.run_pending(), [])

    def test_response_after_pause_is_not_applied(self):
        cycle = self.build(Reply((act(CAP_NOTES),), before=lambda req: self.c.pause_session()), max_rerequest=0)
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "no_decision")
        self.assertEqual(self.world.executed, [])


class ProviderFailureTest(CycleCase):
    def test_provider_error_is_logged_and_nothing_changes(self):
        cycle = self.build(Reply(error=ProviderError("лимит")))
        report = cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("no_decision", "provider_error"))
        self.assertIn("system.error", self.types())
        self.assertNotIn("proposal.received", self.types())

    def test_exhausted_script_is_a_provider_error(self):
        cycle = self.build()
        self.assertEqual(cycle.run(self.due()).reason, "provider_error")
        with self.assertRaises(ScriptExhausted):
            self.provider.propose({"request_id": "x"})

    def test_refusal_is_not_a_decision(self):
        cycle = self.build(Reply((act(CAP_NOTES),), status="refused"))
        report = cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("refused", "refused"))
        self.assertEqual(self.world.executed, [])

    def test_one_repair_attempt_for_a_malformed_answer(self):
        cycle = self.build(Reply(raw={"nonsense": True}), Reply((rest("ок"),)))
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "done")
        first, second = self.provider.calls
        self.assertEqual((first["limits"]["attempt"], second["limits"]["attempt"]), (0, 1))
        self.assertEqual(second["retry_of"], first["request_id"])

    def test_second_malformed_answer_ends_the_cycle(self):
        cycle = self.build(Reply(raw={"nonsense": True}), Reply(raw=[1, 2]))
        report = cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("rejected", "bad_format"))
        self.assertEqual(len(self.provider.calls), 2)

    def test_stale_after_repair_attempt_keeps_retry_link(self):
        move = lambda req: self.c.apply(self.prop(self.c, set_("preferences", "n", 1)))
        cycle = self.build(Reply(raw={"nonsense": True}), Reply((rest("ок"),), before=move), Reply((rest("ок"),)))
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "done")
        self.assertEqual(self.provider.calls[2]["limits"]["attempt"], 1)
        self.assertEqual(self.provider.calls[2]["retry_of"], self.provider.calls[1]["request_id"])


class CreatorRequestTest(CycleCase):
    def test_request_is_recorded_before_delivery_and_delivered_once(self):
        cycle = self.build(Reply((ask("need_plotting", "Подключи графики"),)))
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "done")
        self.assertEqual([r.request_key for r in self.creator.submitted], ["need_plotting"])
        req = self.c.state["world_time"]["world"]["creator_requests"]["need_plotting"]
        self.assertEqual(req["status"], "open")
        self.assertEqual(req["crq_id"], self.creator.submitted[0].crq_id)
        self.assertIn("creator.request", self.types())

    def test_same_unresolved_request_is_not_sent_again(self):
        cycle = self.build(Reply((ask("need_plotting", "Подключи"),)), Reply((ask("need_plotting", "Подключи ещё раз"),)))
        cycle.run(self.due())
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "rejected")
        rej = [e for e in self.c.store.events() if e.type == "proposal.rejected"]
        self.assertEqual(rej[-1].payload["reason"], "duplicate_request")
        self.assertEqual(len(self.creator.submitted), 1)

    def test_bad_request_key_is_rejected(self):
        cycle = self.build(Reply((ask("плохой.ключ", "текст"),)))
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "rejected")
        rej = [e for e in self.c.store.events() if e.type == "proposal.rejected"]
        self.assertEqual(rej[0].payload["reason"], "bad_request_key")

    def test_undelivered_request_is_not_reported_as_delivered(self):
        cycle = self.build(Reply((ask("k1", "помоги"),)), creator=StubCreatorBridge(deliver=False))
        cycle.run(self.due())
        errors = [e for e in self.c.store.events() if e.type == "system.error"]
        self.assertEqual(errors[-1].payload["fault"], "creator_undelivered")

    def test_bridge_failure_is_an_error_not_a_crash(self):
        class Broken:
            def submit(self, request):
                raise OSError("нет связи")

            def poll_replies(self):
                return []
        cycle = self.build(Reply((ask("k2", "помоги"),)), creator=Broken())
        self.assertEqual(cycle.run(self.due()).outcome, "done")
        errors = [e for e in self.c.store.events() if e.type == "system.error"]
        self.assertIn("нет связи", errors[-1].payload["detail"])


class AttentionKindsTest(CycleCase):
    def test_attend_and_no_action_set_attention(self):
        cycle = self.build(Reply((attend("intention", INTENTION),)), Reply((rest("ничего не нужно"),)))
        report = cycle.run(self.due())
        self.assertEqual(self.c.state["affect"]["attention"]["focus"], "intention")
        self.assertEqual(report.names()[-1], "action")
        self.assertEqual(report.stages[-1].status, "skipped")
        cycle.run(self.due())
        att = self.c.state["affect"]["attention"]
        self.assertEqual((att["focus"], att["reason"]), ("rest", "ничего не нужно"))
        self.assertEqual(self.world.executed, [])

    def test_conflicting_proposals_in_one_answer(self):
        cycle = self.build(Reply((attend("intention", INTENTION), rest("и отдохну"))))
        cycle.run(self.due())
        rej = [e for e in self.c.store.events() if e.type == "proposal.rejected"]
        self.assertEqual([e.payload["reason"] for e in rej], ["conflicts_with_accepted"])


if __name__ == "__main__":
    unittest.main()
