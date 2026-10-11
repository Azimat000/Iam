"""StateController.apply: версии, идемпотентность, права, проверки, атомарность (docs/spec/state.md#versioning)."""
import unittest
from unittest import mock

from iam.domain import Op, Source, new_id
from iam.state import (ClockRegression, EventSpec, NotSupported, Proposal, StateController, StateCorrupt, StateError,
                       Violation, verify)
from iam.storage import Store

from .helpers import EXP, PID, StateCase, plain_event, set_


class CreatePersonaTest(StateCase):
    def test_initial_state_and_events(self):
        c = self.controller(created=False)
        self.assertEqual((c.epoch, c.state_version), (1, 0))
        self.assertEqual(c.state["traits"], {})
        res = c.create_persona({"traits": {"curious": {"value": 0.5}}, "world_time": {"world": {"desk": "empty"}}})
        self.assertTrue(res.applied)
        self.assertEqual((c.epoch, c.state_version), (1, 1))
        self.assertEqual(c.state["traits"], {"curious": {"value": 0.5}})
        wt = c.state["world_time"]
        self.assertEqual((wt["session_state"], wt["life_time_ms"], wt["moments"], wt["world"]),
                         ("stopped", 0, {}, {"desk": "empty"}))
        ev = c.store.get_event(1)
        self.assertEqual((ev.type, ev.state_version, ev.source), ("persona.created", 1, Source("operator_panel", "panel")))
        self.assertEqual(ev.causes, (), "корневое событие: само себе основание, в causes его нет")
        self.assertEqual(ev.payload["transition"]["caused_by"], [ev.event_id])
        snap = c.store.latest_snapshot()
        self.assertEqual((snap.epoch, snap.state_version, snap.at_seq), (1, 1, 1))
        self.assertEqual(verify(c.store), [])

    def test_repeat_is_duplicate_and_other_content_is_conflict(self):
        c = self.controller(created=False)
        first = c.create_persona({"traits": {"a": 1}})
        again = c.create_persona({"traits": {"a": 1}})
        self.assertEqual((again.status, again.events[0].seq), ("duplicate", first.events[0].seq))
        self.assertEqual(c.state_version, 1)
        other = c.create_persona({"traits": {"a": 2}})
        self.assertEqual((other.status, other.reason), ("conflict", "idempotency_conflict"))
        self.assertEqual(c.state["traits"], {"a": 1})
        self.assertEqual(self.types(c), ["persona.created", "system.error"])

    def test_world_time_fields_belong_to_controller(self):
        c = self.controller(created=False)
        with self.assertRaises(ValueError):
            c.create_persona({"world_time": {"session_state": "running"}})

    def test_nothing_else_before_creation(self):
        c = self.controller(created=False)
        res = c.apply(self.prop(c, set_("traits", "x", 1), cause="evt_NOTCREATED1", reason="до создания"))
        self.assertEqual(res.reason, "persona_not_created")
        self.assertEqual(c.state_version, 0)

    def test_persona_created_cannot_be_repeated_under_other_key(self):
        c = self.controller()
        carrier = EventSpec("persona.created", "per:other:create", event_id=new_id("evt"))
        p = Proposal(self.tr(c, set_("traits", "x", 1), cause=carrier.event_id), "operator", carrier=carrier)
        self.assertEqual(c.apply(p).reason, "persona_exists")


class ApplyTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)

    def test_unknown_actor_is_a_programmer_error(self):
        with self.assertRaises(ValueError):
            Proposal(self.tr(self.c, set_("traits", "x", 1)), "stranger")

    def test_applied_transition(self):
        c = self.c
        before = c.state_version
        p = self.prop(c, set_("beliefs", "tea.claim", "чай бодрит"), set_("beliefs", "tea.confidence", 0.3),
                      proposal_id="prop_ABCDEFGH")
        res = c.apply(p)
        self.assertEqual((res.status, res.state_version, res.reason), ("applied", before + 1, None))
        self.assertEqual(c.state["beliefs"]["tea"], {"claim": "чай бодрит", "confidence": 0.3})
        carrier = res.events[0]
        self.assertEqual((carrier.type, carrier.state_version, carrier.changes_state), ("state.transition_applied",
                                                                                        before + 1, True))
        self.assertEqual(carrier.idempotency_key, f"trn:{p.transition.transition_id}")
        self.assertEqual(carrier.payload["transition"], p.transition.to_dict())
        self.assertEqual([x["after"] for x in carrier.payload["changes"]], ["чай бодрит", 0.3])
        self.assertEqual(carrier.causes, (self.root(c),))
        snap = c.store.latest_snapshot()
        self.assertEqual((snap.state_version, snap.at_seq, snap.state), (before + 1, carrier.seq, c.state))
        self.assertEqual(verify(c.store), [])

    def test_state_property_is_a_copy(self):
        self.c.state["traits"]["hack"] = 1
        self.assertNotIn("hack", self.c.state["traits"])

    def test_companions_share_the_version(self):
        c = self.c
        mark = EventSpec("proposal.accepted", "prop:prop_ABCDEFGH:accept", {"proposal_id": "prop_ABCDEFGH"})
        res = c.apply(self.prop(c, set_("traits", "x", 1), companions=(mark,)))
        v = c.state_version
        self.assertEqual([(e.type, e.state_version, e.changes_state) for e in res.events],
                         [("state.transition_applied", v, True), ("proposal.accepted", v, True)])
        self.assertEqual(v, 3, "создание, начало сеанса и этот переход: версия растёт на переход, а не на событие")
        self.assertEqual(verify(c.store), [])

    def test_repeat_of_same_proposal_has_no_second_effect(self):
        c = self.c
        p = self.prop(c, Op("affect", "reactions", "add", "радость", True))
        first, second = c.apply(p), c.apply(p)
        self.assertEqual((first.status, second.status), ("applied", "duplicate"))
        self.assertEqual(second.events, first.events)
        self.assertEqual(second.changes, first.changes)
        self.assertEqual(c.state["affect"]["reactions"], ["радость"], "добавлено один раз")
        self.assertEqual(c.state_version, 3)
        self.assertEqual(self.types(c).count("state.transition_applied"), 1)

    def test_same_key_other_content_is_conflict_and_original_untouched(self):
        c = self.c
        p = self.prop(c, set_("traits", "x", 1))
        c.apply(p)
        evil = Proposal(type(p.transition)(p.transition.transition_id, PID, 1, 2, (set_("traits", "x", 666),),
                                           "подмена", p.transition.caused_by), "core")
        for _ in range(2):
            res = c.apply(evil)
            self.assertEqual((res.status, res.reason), ("conflict", "idempotency_conflict"))
        self.assertEqual(c.state["traits"]["x"], 1)
        self.assertEqual(self.types(c).count("system.error"), 1, "одна и та же попытка пишется один раз")
        self.assertEqual(c.store.find_event(key=f"trn:{p.transition.transition_id}").payload["transition"],
                         p.transition.to_dict())

    def test_stale_version_is_refused_and_logged(self):
        c = self.c
        old = self.prop(c, set_("traits", "a", 1))
        c.apply(self.prop(c, set_("traits", "b", 2)))            # состояние ушло вперёд
        before = c.state
        res = c.apply(old)
        self.assertEqual((res.status, res.reason), ("stale", "stale_version"))
        self.assertEqual(c.state, before)
        stale = c.store.find_event(key=f"resp:{old.transition.transition_id}:stale")
        self.assertEqual((stale.type, stale.changes_state, stale.payload["base_version"],
                          stale.payload["current_version"]), ("proposal.stale", False, 2, 3))
        n = len(self.types(c))
        self.assertEqual(c.apply(old).status, "stale")
        self.assertEqual(len(self.types(c)), n, "повтор устаревшего запись не дублирует")

    def test_wrong_epoch_is_stale_even_with_same_version(self):
        c = self.c
        res = c.apply(self.prop(c, set_("traits", "a", 1), base=(2, c.state_version)))
        self.assertEqual(res.status, "stale")

    def test_stale_logged_under_request_id(self):
        c = self.c
        p = self.prop(c, set_("traits", "a", 1), base=(1, 0), request_id="req_ABCDEFGH")
        self.assertEqual(c.apply(p).status, "stale")
        self.assertIsNotNone(c.store.find_event(key="resp:req_ABCDEFGH:stale"))

    def test_parallel_proposals_on_one_base_only_first_wins(self):
        c = self.c
        a, b = self.prop(c, set_("traits", "who", "a")), self.prop(c, set_("traits", "who", "b"))
        self.assertEqual([c.apply(a).status, c.apply(b).status], ["applied", "stale"])
        self.assertEqual(c.state["traits"]["who"], "a")

    def test_rejections_leave_state_and_are_logged(self):
        c = self.c
        base = c.state
        cases = {
            "invalid_transition": self.prop(c, set_("traits", "x", 1), reason="ab"),
            "wrong_persona": Proposal(type(self.tr(c, set_("traits", "x", 1)))(
                new_id("trn"), EXP, 1, 2, (set_("traits", "x", 1),), "чужая", (self.root(c),)), "core"),
            "bad_carrier": self.prop(c, set_("traits", "x", 1), carrier=EventSpec("input.arrived", "rx:tg:1")),
            "bad_carrier2": self.prop(c, set_("traits", "x", 1), carrier=EventSpec("no.such", "k:k:k")),
            "payload_reserved": self.prop(c, set_("traits", "x", 1),
                                          carrier=EventSpec("state.transition_applied", "trn:trn_ABCDEFGH",
                                                            {"changes": 1})),
            "unknown_cause": self.prop(c, set_("traits", "x", 1), cause="evt_UNKNOWN001"),
            "bad_op": self.prop(c, Op("traits", "y", "remove")),
            "bad_value": self.prop(c, set_("traits", "x", {1, 2})),
        }
        got = {}
        for name, p in cases.items():
            res = c.apply(p)
            got[name] = res.reason
            self.assertEqual((res.status, c.state, c.state_version), ("rejected", base, 2))
        self.assertEqual(got, {"invalid_transition": "invalid_transition", "wrong_persona": "wrong_persona",
                               "bad_carrier": "bad_carrier", "bad_carrier2": "bad_carrier",
                               "payload_reserved": "payload_reserved", "unknown_cause": "unknown_cause",
                               "bad_op": "path_missing", "bad_value": "bad_value"})
        self.assertEqual(c.state, base)
        self.assertEqual(c.state_version, 2)
        self.assertGreaterEqual(self.types(c).count("system.error"), len(cases) - 1)

    def test_rejection_of_model_proposal_uses_proposal_rejected(self):
        c = self.c
        p = self.prop(c, Op("traits", "ghost", "remove"), actor="model", proposal_id="prop_REJECT001", cause=self.root(c))
        res = c.apply(p)
        self.assertEqual(res.reason, "path_missing")
        ev = c.store.find_event(key="prop:prop_REJECT001:reject")
        self.assertEqual((ev.type, ev.changes_state, ev.payload["reason"]), ("proposal.rejected", False, "path_missing"))
        self.assertEqual(ev.source, Source("core", "model:state_controller"))
        n = len(self.types(c))
        c.apply(p)
        self.assertEqual(len(self.types(c)), n, "повтор отказа запись не дублирует")

    def test_structural_rejection_never_uses_untrusted_proposal_id(self):
        c = self.c
        t = self.tr(c, set_("traits", "x", 1), proposal_id="prop_has:colon", reason="ab")
        res = c.apply(Proposal(t, "core"))
        self.assertEqual(res.reason, "invalid_transition")
        self.assertIsNone(c.store.find_event(key="prop:prop_has:colon:reject"))

    def test_actor_rights(self):
        c = self.c
        carrier = EventSpec("operator.intervention", "opr:opr_ABCDEFGH", event_id="evt_SELFCAUSE1")
        self.assertEqual(c.apply(Proposal(self.tr(c, set_("traits", "x", 1), cause="evt_SELFCAUSE1"), "model",
                                          carrier=carrier)).reason, "actor_forbidden")
        fresh = StateController(Store.create(self.tmp / "f.db", "per_FreshPersona1"), clock=self.clock())
        self.addCleanup(fresh.store.close)
        carrier = EventSpec("persona.created", "per:per_FreshPersona1:create", event_id="evt_SELFCAUSE2")
        t = self.tr(fresh, set_("traits", "x", 1), cause="evt_SELFCAUSE2")
        self.assertEqual(fresh.apply(Proposal(t, "core", carrier=carrier)).reason, "actor_forbidden",
                         "создавать личность — только оператору")
        # у остальных типов состояние меняют core, model и operator, но не world
        self.assertEqual(c.apply(self.prop(c, set_("traits", "x", 1), actor="world")).reason, "actor_forbidden")

    def test_session_and_creation_events_are_written_only_by_controller_methods(self):
        c = self.c
        for t, key in (("session.started", "ses:ses_FORGED0001:start"), ("session.paused", "ses:ses_FORGED0001:pause:1"),
                       ("time.moment_due", "due:mom_FORGED001:1")):
            res = c.apply(self.prop(c, set_("traits", "x", 1), carrier=EventSpec(t, key)))
            self.assertEqual(res.reason, "carrier_reserved", t)
        companion = EventSpec("session.resumed", "ses:ses_FORGED0001:resume:1")
        self.assertEqual(c.apply(self.prop(c, set_("traits", "x", 1), companions=(companion,))).reason, "carrier_reserved")
        self.assertEqual(c.session_state, "running")

    def test_self_cause_only_for_operator_and_core(self):
        c = self.c
        for actor, ok in (("operator", True), ("core", True), ("world", False), ("model", False)):
            ev_id = new_id("evt")
            carrier = EventSpec("action.confirmed" if actor == "world" else "state.transition_applied",
                                f"act:act_{ev_id[4:]}:ok" if actor == "world" else f"trn:trn_{ev_id[4:]}",
                                event_id=ev_id)
            res = c.apply(Proposal(self.tr(c, set_("traits", f"k{actor}", 1), cause=ev_id), actor, carrier=carrier))
            self.assertEqual(res.applied, ok, actor)
            if not ok:
                self.assertEqual(res.reason, "unknown_cause")

    def test_protected_fields_cannot_be_written_through_apply(self):
        c = self.c
        for path in ("session_state", "life_time_ms", "moments.mom_AAAAAAAA", "session_id", "pause_count"):
            res = c.apply(self.prop(c, set_("world_time", path, 5), actor="operator"))
            self.assertEqual(res.reason, "protected_field", path)
        self.assertTrue(c.apply(self.prop(c, set_("world_time", "world.desk", "ok"))).applied, "world открыт")

    def test_session_rules_for_carriers(self):
        c = self.c
        c.pause_session()
        res = c.apply(self.prop(c, set_("traits", "x", 1)))
        self.assertEqual(res.reason, "session_forbids")
        self.assertEqual(c.state["traits"].get("x"), None)

    def test_state_machine_and_confirmation_for_done(self):
        c = self.c
        c.apply(self.prop(c, set_("intentions", "int_AAAAAAAA", {"text": "дописать", "status": "active"})))
        res = c.apply(self.prop(c, Op("intentions", "int_AAAAAAAA.status", "transition", "done", True)))
        self.assertEqual(res.reason, "needs_confirmation", "«выполнено» без подтверждённого результата недопустимо")
        confirmed = c.apply(Proposal(self.tr(c, set_("world_time", "world.note", "написана")), "world",
                                     carrier=EventSpec("action.confirmed", "act:act_ABCDEFGH:ok")))
        self.assertTrue(confirmed.applied, confirmed.violations)
        res = c.apply(self.prop(c, Op("intentions", "int_AAAAAAAA.status", "transition", "done", True),
                                cause=confirmed.events[0].event_id))
        self.assertTrue(res.applied, res.violations)
        self.assertEqual(c.state["intentions"]["int_AAAAAAAA"]["status"], "done")
        again = c.apply(self.prop(c, Op("intentions", "int_AAAAAAAA.status", "transition", "active", True)))
        self.assertEqual(again.reason, "bad_transition")

    def test_status_cannot_be_changed_around_the_state_machine(self):
        """Автомат нельзя обойти записью статуса напрямую — ни моделью, ни оператором, ни записью целого родителя."""
        c = self.c
        c.apply(self.prop(c, set_("intentions", "int_AAAAAAAA", {"text": "дописать", "status": "active"})))
        status = "int_AAAAAAAA.status"
        attempts = {
            "model set done": self.prop(c, set_("intentions", "int_AAAAAAAA.status", "done"), actor="model"),
            "operator set done": self.prop(c, set_("intentions", "int_AAAAAAAA.status", "done"), actor="operator"),
            "replace record": self.prop(c, set_("intentions", "int_AAAAAAAA", {"text": "x", "status": "done"})),
            "replace group": self.prop(c, set_("intentions", "int_AAAAAAAA", {"text": "x", "status": "active"}),
                                       set_("intentions", "int_BBBBBBBB", {"status": "done"})),
            "create finished": self.prop(c, set_("intentions", "int_CCCCCCCC", {"status": "done"})),
            "delete record": self.prop(c, Op("intentions", "int_AAAAAAAA", "remove")),
            "set then finish in one": self.prop(c, set_("intentions", "int_DDDDDDDD", {"status": "active"}),
                                                Op("intentions", "int_DDDDDDDD.status", "transition", "done", True)),
        }
        for name, p in attempts.items():
            res = c.apply(p)
            expected = "needs_confirmation" if name == "set then finish in one" else "machine_bypass"
            self.assertEqual(res.reason, expected, f"{name}: {res.violations}")
        r = c.apply(self.prop(c, Op("intentions", status, "add", "x", True)))
        self.assertEqual(r.reason, "not_a_list")
        self.assertEqual(c.state["intentions"]["int_AAAAAAAA"]["status"], "active")
        # допустимое: создать с начального статуса, менять другие поля записи, переводить автоматом
        self.assertTrue(c.apply(self.prop(c, set_("intentions", "int_AAAAAAAA.text", "переписать"))).applied)
        self.assertTrue(c.apply(self.prop(c, set_("intentions", "int_EEEEEEEE", {"status": "active"}))).applied)
        self.assertTrue(c.apply(self.prop(c, set_("intentions", "note", "не запись намерения"))).applied,
                        "не-словарь на месте записи автомату не подчиняется")
        self.assertTrue(c.apply(self.prop(c, Op("intentions", status, "transition", "deferred", True))).applied)
        again = c.apply(self.prop(c, set_("intentions", "int_AAAAAAAA.status", "active")))
        self.assertEqual(again.reason, "machine_bypass", "возврат из deferred только через transition")

    def test_companion_events_need_the_same_rights_as_carrier(self):
        c = self.c
        fake = EventSpec("action.confirmed", "act:act_FORGED001:ok", {"result": "готово"})
        res = c.apply(self.prop(c, set_("traits", "x", 1), actor="model", companions=(fake,)))
        self.assertEqual(res.reason, "actor_forbidden")
        self.assertIsNone(c.store.find_event(key="act:act_FORGED001:ok"))
        intervention = EventSpec("operator.intervention", "opr:opr_FORGED001")
        self.assertEqual(c.apply(self.prop(c, set_("traits", "x", 1), actor="model", companions=(intervention,))).reason,
                         "actor_forbidden")

    def test_operator_intervention_only_through_intervene(self):
        c = self.c
        carrier = EventSpec("operator.intervention", "opr:opr_FORGED002", {"kind": "grant_capability"})
        res = c.apply(self.prop(c, set_("traits", "x", 1), actor="operator", carrier=carrier))
        self.assertEqual(res.reason, "carrier_reserved")

    def test_validators_get_copies(self):
        c = self.c

        def vandal(ch):
            ch.before["traits"]["curious"]["value"] = 99
            ch.after["traits"]["injected"] = 1
            ch.changes[0]["after"] = "подмена"
            return []
        c.add_validator("vandal", vandal)
        res = c.apply(self.prop(c, set_("beliefs", "b", 1)))
        self.assertTrue(res.applied)
        self.assertEqual(c.state["traits"], {"curious": {"value": 0.5}})
        self.assertEqual(res.changes[0]["after"], 1)
        self.assertEqual(verify(c.store), [])

    def test_custom_validators(self):
        c = self.c
        c.add_validator("range", lambda ch: [f"{x['path']}: вне 0..1" for x in ch.changes
                                              if x["group"] == "affect" and not 0 <= x["after"] <= 1])
        seen = []
        c.add_validator("spy", lambda ch: seen.append((ch.actor, ch.carrier_type, ch.before["affect"], ch.after["affect"])) or [])
        res = c.apply(self.prop(c, set_("affect", "load", 1.5)))
        self.assertEqual((res.reason, res.violations[0].message), ("validator:range", "load: вне 0..1"))
        self.assertEqual(c.state["affect"], {})
        self.assertTrue(c.apply(self.prop(c, set_("affect", "load", 0.5))).applied)
        self.assertEqual(seen, [("core", "state.transition_applied", {}, {"load": 1.5}),
                                ("core", "state.transition_applied", {}, {"load": 0.5})])
        c.apply(self.prop(c, Op("affect", "ghost", "remove")))
        self.assertEqual(len(seen), 2, "если операции неприменимы, валидаторы не вызываются")

    def test_validators_passed_to_constructor(self):
        c = self.controller(validators=[("always_no", lambda ch: ["нет"])]) if False else None
        store = Store.create(self.tmp / "v.db", "per_ValidatorsOne")
        self.addCleanup(store.close)
        c = StateController(store, clock=self.clock(), validators=[("no", lambda ch: ["нет"])])
        self.assertEqual(c.create_persona().reason, "validator:no")

    def test_invalid_event_and_taken_companion_key(self):
        c = self.c
        bad_key = EventSpec("proposal.accepted", "wrong-key")
        self.assertEqual(c.apply(self.prop(c, set_("traits", "x", 1), companions=(bad_key,))).reason, "invalid_event")
        mark = EventSpec("proposal.accepted", "prop:prop_TAKEN0001:accept")
        self.assertTrue(c.apply(self.prop(c, set_("traits", "x", 1), companions=(mark,))).applied)
        res = c.apply(self.prop(c, set_("traits", "y", 1), companions=(mark,)))
        self.assertEqual(res.reason, "key_taken")
        self.assertNotIn("y", c.state["traits"])

    def test_atomic_rollback_on_failure(self):
        c = self.c
        base, seq = c.state, c.store.last_seq()
        mark = EventSpec("proposal.accepted", "prop:prop_ATOMIC001:accept")
        with mock.patch.object(c.store, "put_snapshot", side_effect=RuntimeError("диск отказал")):
            with self.assertRaises(RuntimeError):
                c.apply(self.prop(c, set_("traits", "x", 1), companions=(mark,)))
        self.assertEqual((c.state, c.state_version, c.store.last_seq()), (base, 2, seq),
                         "ни события, ни части перехода не осталось, память перечитана из базы")
        self.assertTrue(c.apply(self.prop(c, set_("traits", "x", 1))).applied)

    def test_event_that_was_not_really_written_aborts_the_whole_transition(self):
        c = self.c
        base, seq = c.state, c.store.last_seq()
        real = c.store.append_event
        with mock.patch.object(c.store, "append_event", side_effect=lambda ev: (real(ev)[0], False)):
            with self.assertRaises(StateCorrupt):
                c.apply(self.prop(c, set_("traits", "x", 1)))
        self.assertEqual((c.state, c.store.last_seq()), (base, seq), "переход откатан целиком")

    def test_orphan_state_event_is_corruption(self):
        c = self.c
        c.store.append_event(plain_event(0).__class__(**{**plain_event(0).__dict__, "idempotency_key": "ntc:inp_ABCDEFGH:N1",
                                                         "type": "input.noticed", "changes_state": True,
                                                         "event_id": new_id("evt")}))
        with self.assertRaises(StateCorrupt):
            StateController(c.store, clock=self.clock(), recover=False)

    def test_snapshot_with_foreign_groups_is_corruption(self):
        c = self.c
        from iam.storage import Snapshot
        last = c.store.get_event(c.store.last_seq())
        c.store.put_snapshot(Snapshot(1, 3, last.seq, 0, "2026-10-11T10:00:00.000Z", {"traits": {}}))
        with self.assertRaises(StateCorrupt):
            StateController(c.store, clock=self.clock(), recover=False)

    def test_life_time_never_decreases_along_seq(self):
        c = self.c
        c.start_session()
        for i in range(5):
            self.time.advance(100)
            c.apply(self.prop(c, set_("traits", f"k{i}", i)))
            c.record_event(EventSpec("input.arrived", f"rx:tg:{i}"))
        lives = [e.life_time_ms for e in c.store.events()]
        self.assertEqual(lives, sorted(lives))
        self.assertGreater(lives[-1], 0)
        self.assertEqual(verify(c.store), [])

    def test_clock_behind_journal_is_refused(self):
        c = self.c
        c.start_session()
        self.time.advance(1000)
        c.pause_session()
        c.store.close()
        store = Store.open(self.db)
        self.addCleanup(store.close)
        with self.assertRaises(ClockRegression):
            StateController(store, clock=self.clock(0))     # часы начали с нуля, а в журнале уже 1000
        stuck = StateController(store, clock=self.clock(5000))
        stuck._last_life = 9999
        with self.assertRaises(ClockRegression):
            stuck.record_event(EventSpec("input.arrived", "rx:tg:77"))

    def test_permanent_persona_refuses_time_scale(self):
        store = Store.create(self.tmp / "x.db", "per_ScaledPersona")
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            StateController(store, clock=self.clock(time_scale=10))
        exp = Store.create(self.tmp / "e.db", EXP)
        self.addCleanup(exp.close)
        StateController(exp, clock=self.clock(time_scale=10))     # экспериментальному можно

    def test_readonly_controller_reads_but_cannot_write(self):
        self.c.start_session()
        self.c.store.close()
        store = Store.open(self.db, readonly=True)
        self.addCleanup(store.close)
        c = StateController(store, clock=self.clock())
        self.assertEqual((c.state_version, c.session_state), (2, "running"), "в режиме чтения аварию не «чинит»")
        for call in (lambda: c.apply(self.prop(c, set_("traits", "x", 1))), lambda: c.start_session(),
                     lambda: c.record_event(EventSpec("input.arrived", "rx:tg:1")), lambda: c.create_persona()):
            with self.assertRaises(StateError):
                call()


class RecordEventTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller()

    def test_record_duplicate_and_conflict(self):
        c = self.c
        spec = EventSpec("input.arrived", "rx:tg:42", {"text": "привет"})
        first, again = c.record_event(spec, actor="core", actor_ref="receiver"), c.record_event(spec)
        self.assertEqual((first.status, again.status), ("recorded", "duplicate"))
        self.assertEqual(again.event.seq, first.event.seq)
        self.assertEqual(first.event.state_version, c.state_version, "версию такое событие не меняет")
        self.assertEqual(c.state_version, 1)
        clash = c.record_event(EventSpec("input.arrived", "rx:tg:42", {"text": "другое"}))
        self.assertEqual((clash.status, clash.reason), ("conflict", "idempotency_conflict"))
        self.assertEqual(c.store.find_event(key="rx:tg:42").payload, {"text": "привет"})

    def test_state_changing_or_unknown_type_is_programmer_error(self):
        for t in ("input.noticed", "state.transition_applied", "no.such.type"):
            with self.assertRaises(ValueError):
                self.c.record_event(EventSpec(t, "x:y:z"))

    def test_invalid_event_is_refused_and_logged(self):
        res = self.c.record_event(EventSpec("input.arrived", "bad key"))
        self.assertEqual((res.status, res.reason), ("rejected", "invalid_event"))
        self.assertEqual(self.types(self.c)[-1], "system.error")

    def test_pause_allows_arrival_but_not_perception_or_requests(self):
        c = self.c
        c.start_session()
        c.pause_session()
        self.assertEqual(c.record_event(EventSpec("input.arrived", "rx:tg:5")).status, "recorded")
        self.assertEqual(c.record_event(EventSpec("system.backup_created", "bak:snp_ABCDEFGH")).status, "recorded")
        res = c.record_event(EventSpec("memory.retrieved", "rtv:cyc_ABCDEFGH:1"))
        self.assertEqual((res.status, res.reason), ("rejected", "session_forbids"))

    def test_request_and_response_events_only_through_their_methods(self):
        for t, k in (("proposal.requested", "req:req_ABCDEFGH"), ("proposal.received", "resp:req_ABCDEFGH")):
            with self.assertRaises(ValueError):
                self.c.record_event(EventSpec(t, k, {}))


if __name__ == "__main__":
    unittest.main()
