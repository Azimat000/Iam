"""Расширения привратника под цикл (D-014): разборщики видов предложений, автомат исхода действия,
права на запись исходов, чтение журнала."""
import unittest

from iam.domain import Op, new_id
from iam.state import EventSpec, KindDecision, Violation

from state.test_response import ResponseCase, proposal

from .helpers import CAP_NOTES, set_

AID = "act_Machine0001"
ACTION = {"status": "accepted", "kind": "action", "capability_id": CAP_NOTES, "params": {}, "proposal_id": "prop_Machine0001",
          "cycle_id": "cyc_Machine0001"}


def kind_proposal(kind, body, pid=None):
    return proposal(None, pid=pid, kind=kind, body=body)


class KindHandlerTest(ResponseCase):
    def test_state_change_cannot_be_overridden(self):
        with self.assertRaises(ValueError):
            self.c.register_kind("state_change", lambda pr, ctx: KindDecision())

    def test_kind_without_handler_is_not_supported(self):
        req = self.ask(allowed_proposals=["no_action", "state_change"])
        res = self.c.receive_response(self.answer(req, [kind_proposal("no_action", {"reason": "тихо"})]))
        self.assertEqual((res.status, list(res.rejected.values())), ("rejected", ["kind_not_supported"]))

    def test_handler_decides_ops_and_sees_the_context(self):
        seen = []

        def handler(pr, ctx):
            seen.append((ctx.request["purpose"], ctx.accepted_kinds, ctx.life_time_ms, "affect" in ctx.state))
            return KindDecision((set_("preferences", "rested", pr["body"]["reason"]),))
        self.c.register_kind("no_action", handler)
        req = self.ask()
        res = self.c.receive_response(self.answer(req, [kind_proposal("no_action", {"reason": "тихо"})]))
        self.assertEqual(res.status, "applied")
        self.assertEqual(seen, [("choose_action", (), self.c.life_time_ms, True)])
        self.assertEqual(self.c.state["preferences"]["rested"], "тихо")

    def test_handler_violation_rejects_that_proposal_only(self):
        self.c.register_kind("no_action", lambda pr, ctx: KindDecision(violation=Violation("nope", "нельзя")))
        req = self.ask()
        ok = proposal([{"group": "preferences", "path": "a", "op": "set", "value": 1}])
        bad = kind_proposal("no_action", {"reason": "x"})
        res = self.c.receive_response(self.answer(req, [bad, ok]))
        self.assertEqual((res.status, res.rejected), ("partial", {bad["proposal_id"]: "nope"}))
        self.assertEqual(self.c.state["preferences"]["a"], 1)

    def test_kind_must_be_allowed_by_the_request_even_for_state_change(self):
        self.c.register_kind("no_action", lambda pr, ctx: KindDecision((set_("preferences", "z", 1),)))
        req = self.ask(allowed_proposals=["no_action"])
        change = proposal([{"group": "preferences", "path": "a", "op": "set", "value": 1}])
        res = self.c.receive_response(self.answer(req, [change]))
        self.assertEqual(list(res.rejected.values()), ["kind_not_allowed"])
        self.assertNotIn("a", self.c.state["preferences"])

    def test_handler_ops_still_pass_the_gatekeeper_checks(self):
        """Разборщик не обходит защиту: поля сеанса и времени остаются недоступны."""
        self.c.register_kind("no_action", lambda pr, ctx: KindDecision((set_("world_time", "session_state", "stopped"),)))
        req = self.ask()
        res = self.c.receive_response(self.answer(req, [kind_proposal("no_action", {"reason": "x"})]))
        self.assertEqual(list(res.rejected.values()), ["protected_field"])
        self.assertEqual(self.c.session_state, "running")

    def test_journal_reading_helpers(self):
        last = self.c.store.last_seq()
        self.ask()
        later = self.c.events_after(last)
        self.assertEqual([e.type for e in later], ["proposal.requested"])
        self.assertEqual(self.c.find_event(key=later[0].idempotency_key).event_id, later[0].event_id)
        self.assertEqual(self.c.find_event(event_id=later[0].event_id).seq, later[0].seq)
        self.assertIsNone(self.c.find_event(key="req:req_NoSuchOne"))
        self.assertEqual(self.c.events_after(self.c.store.last_seq()), [])


class ActionMachineTest(ResponseCase):
    """Исход действия — конечный автомат: принято → попытка → ровно один исход; кто что вправе писать."""

    def accepted(self):
        self.assertTrue(self.c.apply(self.prop(self.c, set_("world_time", f"world.actions.{AID}", dict(ACTION)))).applied)

    def move(self, status, event_type, key, *, actor, cause_type=None, to=None):
        """Попробовать перевод статуса; основание — событие нужного типа (запись в журнале)."""
        cause = self.root(self.c)
        if cause_type:
            self.c.record_event(EventSpec("memory.retrieved", f"rtv:{new_id('cyc')}:0"))
        return self.c.apply(self.prop(
            self.c, Op("world_time", f"world.actions.{AID}.status", "transition", status, True), actor=actor, cause=cause,
            carrier=EventSpec(event_type, key)))

    def test_creation_only_as_accepted(self):
        res = self.c.apply(self.prop(self.c, set_("world_time", f"world.actions.{AID}", {**ACTION, "status": "confirmed"})))
        self.assertEqual((res.status, res.reason), ("rejected", "machine_bypass"))

    def test_cannot_skip_the_attempt(self):
        self.accepted()
        res = self.move("confirmed", "action.confirmed", f"act:{AID}:ok", actor="world")
        self.assertEqual((res.status, res.reason), ("rejected", "bad_transition"))

    def test_attempt_needs_an_accepted_proposal_as_its_cause(self):
        self.accepted()
        res = self.move("attempted", "action.attempted", f"act:{AID}:try", actor="core")
        self.assertEqual((res.status, res.reason), ("rejected", "needs_confirmation"))

    def test_outcome_needs_the_attempt_as_its_cause(self):
        """Исход без ссылки на попытку (событие-основание — не попытка) не принимается."""
        self.accepted()
        req = self.ask(allowed_proposals=["state_change"])
        got = self.c.receive_response(self.answer(req, [proposal([{"group": "preferences", "path": "q", "op": "set", "value": 1}])]))
        accepted = next(e for e in got.transition.events if e.type == "proposal.accepted")
        up = self.c.apply(self.prop(self.c, Op("world_time", f"world.actions.{AID}.status", "transition", "attempted", True),
                                    cause=accepted.event_id, carrier=EventSpec("action.attempted", f"act:{AID}:try")))
        self.assertTrue(up.applied, up)
        res = self.c.apply(self.prop(self.c, Op("world_time", f"world.actions.{AID}.status", "transition", "confirmed", True),
                                     actor="world", cause=accepted.event_id, carrier=EventSpec("action.confirmed", f"act:{AID}:ok")))
        self.assertEqual((res.status, res.reason), ("rejected", "needs_confirmation"))

    def test_outcome_must_be_carried_by_its_own_event(self):
        """Даже с верными основаниями статус не получить переходом, который несёт общее событие."""
        self.accepted()
        req = self.ask(allowed_proposals=["state_change"])
        got = self.c.receive_response(self.answer(req, [proposal([{"group": "preferences", "path": "q", "op": "set", "value": 1}])]))
        accepted = next(e for e in got.transition.events if e.type == "proposal.accepted")
        res = self.c.apply(self.prop(self.c, Op("world_time", f"world.actions.{AID}.status", "transition", "attempted", True),
                                     cause=accepted.event_id))
        self.assertEqual((res.status, res.reason), ("rejected", "wrong_carrier"))
        up = self.c.apply(self.prop(self.c, Op("world_time", f"world.actions.{AID}.status", "transition", "attempted", True),
                                    cause=accepted.event_id, carrier=EventSpec("action.attempted", f"act:{AID}:try")))
        self.assertTrue(up.applied)
        attempted = up.events[0].event_id
        for actor in ("core", "world"):
            res = self.c.apply(self.prop(self.c, Op("world_time", f"world.actions.{AID}.status", "transition", "confirmed", True),
                                         actor=actor, cause=attempted))
            self.assertEqual(res.status, "rejected", actor)
            self.assertIn("wrong_carrier", {v.code for v in res.violations}, actor)

    def test_model_state_change_cannot_touch_action_records(self):
        self.accepted()
        req = self.ask(allowed_proposals=["state_change"])
        for path in (f"world.actions.{AID}.outcome", "world.actions", "world", "world.creator_requests.k1"):
            bad = proposal([{"group": "world_time", "path": path, "op": "set", "value": {}}])
            res = self.c.receive_response(self.answer(req, [bad]))
            self.assertEqual(list(res.rejected.values()), ["reserved_path"], path)
            req = self.ask(allowed_proposals=["state_change"])

    def test_only_the_world_confirms(self):
        self.accepted()
        for actor in ("core", "model", "operator"):
            res = self.c.apply(self.prop(self.c, set_("preferences", "x", 1), actor=actor,
                                         carrier=EventSpec("action.confirmed", f"act:{AID}:ok")))
            self.assertEqual((res.status, res.reason), ("rejected", "actor_forbidden"), actor)

    def test_message_sent_belongs_to_the_world_and_unknown_to_core_or_world(self):
        for n, (actor, ok) in enumerate((("core", False), ("model", False), ("world", True))):
            res = self.c.apply(self.prop(self.c, set_("preferences", f"s_{actor}", 1), actor=actor,
                                         carrier=EventSpec("message.out_sent", f"out:act_Sent0000{n}:ok")))
            self.assertEqual(res.applied, ok, (actor, res.reason))
        for n, (actor, ok) in enumerate((("core", True), ("world", True), ("model", False))):
            res = self.c.apply(self.prop(self.c, set_("preferences", f"u_{actor}", 1), actor=actor,
                                         carrier=EventSpec("message.out_unknown", f"out:act_Unkn0000{n}:unk")))
            self.assertEqual(res.applied, ok, (actor, res.reason))


if __name__ == "__main__":
    unittest.main()
