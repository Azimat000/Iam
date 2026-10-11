"""Запрос и ответ модели: эхо идентификатора и версии, устаревание, повтор, проверка каждого предложения
(docs/spec/envelopes.md; сценарий «Повтор результата или старый ответ», R-107, R-108, R-183)."""
import unittest

from iam.domain import new_id
from iam.state import EventSpec, verify

from .helpers import PID, StateCase


def op(group, path, kind="set", *value):
    d = {"group": group, "path": path, "op": kind}
    if value:
        d["value"] = value[0]
    return d


def proposal(ops, *, pid=None, cites=(), kind="state_change", body=None):
    return {"proposal_id": pid or new_id("prop"), "kind": kind, "rationale": "потому что", "cites": list(cites),
            "body": body if body is not None else {"ops": ops}}


class ResponseCase(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)

    def request(self, c=None, *, deadline=60_000, **over) -> dict:
        c = c or self.c
        rid = new_id("req")
        req = {"request_id": rid, "persona_id": PID, "session_id": c.session_id, "epoch": c.epoch,
               "state_version": c.state_version, "issued_at": "2026-10-11T10:00:00.000Z",
               "life_time_ms": c.life_time_ms, "purpose": "choose_action", "context": [],
               "allowed_proposals": ["state_change", "no_action"], "allowed_actions": [],
               "limits": {"max_output_tokens": 500, "deadline_ms": deadline, "attempt": 0},
               "response_schema": "response.schema.json", "idempotency_key": f"req:{rid}", "epoch": c.epoch,
               "cycle_id": new_id("cyc")}
        req.update(over)
        return req

    def ask(self, **kw) -> dict:
        req = self.request(**kw)
        res = self.c.record_request(req)
        self.assertEqual(res.status, "recorded", res)
        return req

    @staticmethod
    def answer(req: dict, proposals, **over) -> dict:
        resp = {"request_id": req["request_id"], "epoch": req["epoch"], "state_version": req["state_version"],
                "provider": {"name": "stub", "model": "stub-1"}, "status": "ok", "proposals": proposals,
                "explanation": "объяснение", "usage": {"calls": 1, "duration_ms": 10}, "tool_use_detected": False}
        resp.update(over)
        return resp


class RequestTest(ResponseCase):
    def test_request_is_recorded_before_sending(self):
        req = self.request()
        res = self.c.record_request(req)
        self.assertEqual((res.status, res.event.type, res.event.payload["request"]["request_id"]),
                         ("recorded", "proposal.requested", req["request_id"]))
        self.assertFalse(res.event.changes_state)
        self.assertEqual(self.c.record_request(req).status, "duplicate")

    def test_bad_requests_are_refused(self):
        c = self.c
        bad = self.request()
        del bad["state_version"]
        self.assertEqual(c.record_request(bad).reason, "bad_request")
        self.assertEqual(c.record_request(self.request(persona_id="per_SomebodyElse")).reason, "wrong_context")
        self.assertEqual(c.record_request(self.request(session_id=new_id("ses"))).reason, "wrong_context")

    def test_no_request_in_pause(self):
        self.c.pause_session()
        self.assertEqual(self.c.record_request(self.request(session_id=self.c.session_id)).reason, "session_forbids")


class AcceptTest(ResponseCase):
    def test_accepted_proposals_become_one_transition(self):
        c = self.c
        req = self.ask()
        v = c.state_version
        p1 = proposal([op("beliefs", "tea.claim", "set", "чай любят с мятой")], cites=[self.root(c)])
        p2 = proposal([op("affect", "load", "set", 0.2), op("affect", "mood", "set", {"valence": 0.1})])
        res = c.receive_response(self.answer(req, [p1, p2]))
        self.assertEqual((res.status, res.accepted), ("applied", (p1["proposal_id"], p2["proposal_id"])))
        self.assertEqual(c.state_version, v + 1, "несколько предложений ответа — одна новая версия (E-10)")
        self.assertEqual(c.state["beliefs"]["tea"]["claim"], "чай любят с мятой")
        self.assertEqual(c.state["affect"]["load"], 0.2)
        events = [e for e in c.store.events() if e.seq > res.received.seq - 1]
        self.assertEqual([e.type for e in events],
                         ["proposal.received", "state.transition_applied", "proposal.accepted", "proposal.accepted"])
        self.assertEqual({e.state_version for e in events[1:]}, {v + 1})
        self.assertEqual(events[0].idempotency_key, f"resp:{req['request_id']}")
        trn = events[1].payload["transition"]
        self.assertIn(res.received.event_id, trn["caused_by"])
        self.assertIn(self.root(c), trn["caused_by"], "основания из ссылок предложения попадают в причины перехода")
        self.assertNotIn("proposal_id", trn, "предложений два — единственного proposal_id нет")
        self.assertEqual(events[1].source.ref, "model:stub")
        self.assertEqual(verify(c.store), [])

    def test_citing_a_memory_record_is_allowed(self):
        from iam.domain import Op
        from .helpers import memory
        c = self.c
        c.apply(self.prop(c, Op("memory", "mem_AAAAAAAA", "add", memory(), True)))
        res = c.receive_response(self.answer(self.ask(), [proposal([op("traits", "a", "set", 1)], cites=["mem_AAAAAAAA"])]))
        self.assertEqual(res.status, "applied")

    def test_single_proposal_keeps_its_id(self):
        c = self.c
        req = self.ask()
        p = proposal([op("traits", "bold", "set", 1)])
        res = c.receive_response(self.answer(req, [p]))
        self.assertEqual(res.transition.events[0].payload["transition"]["proposal_id"], p["proposal_id"])

    def test_same_answer_twice_is_applied_once(self):
        """Сценарий «Повтор результата или старый ответ»: дубликат ответа не применяется второй раз."""
        c = self.c
        req = self.ask()
        resp = self.answer(req, [proposal([op("affect", "reactions", "add", "радость")])])
        first, second = c.receive_response(resp), c.receive_response(resp)
        self.assertEqual((first.status, second.status), ("applied", "duplicate"))
        self.assertEqual(second.received.seq, first.received.seq)
        self.assertEqual(c.state["affect"]["reactions"], ["радость"])
        self.assertEqual(self.types(c).count("proposal.accepted"), 1)
        self.assertEqual(self.types(c).count("proposal.received"), 1)

    def test_same_request_with_other_content_is_conflict(self):
        c = self.c
        req = self.ask()
        c.receive_response(self.answer(req, [proposal([op("traits", "a", "set", 1)])]))
        res = c.receive_response(self.answer(req, [proposal([op("traits", "a", "set", 2)])]))
        self.assertEqual((res.status, res.reason), ("conflict", "response_conflict"))
        self.assertEqual(c.state["traits"]["a"], 1)

    def test_confirmed_action_allows_done_through_citation(self):
        c = self.c
        c.apply(self.prop(c, op_set("intentions", "int_BBBBBBBB", {"text": "написать", "status": "active"})))
        done = op("intentions", "int_BBBBBBBB.status", "transition", "done")
        req = self.ask()
        res = c.receive_response(self.answer(req, [proposal([done], pid="prop_NOCONFIRM")]))
        self.assertEqual(res.rejected, {"prop_NOCONFIRM": "needs_confirmation"})
        from iam.domain import Op
        from iam.state import Proposal
        confirmed = c.apply(Proposal(self.tr(c, Op("world_time", "world.note", "set", "готово", True)), "world",
                                     carrier=EventSpec("action.confirmed", "act:act_ABCDEFGH:ok")))
        req2 = self.ask()
        ok = c.receive_response(self.answer(req2, [proposal([done], cites=[confirmed.events[0].event_id])]))
        self.assertEqual(ok.status, "applied", ok)
        self.assertEqual(c.state["intentions"]["int_BBBBBBBB"]["status"], "done")


def op_set(group, path, value):
    from iam.domain import Op
    return Op(group, path, "set", value, True)


class StaleTest(ResponseCase):
    def test_old_version_answer_is_stale_and_not_applied(self):
        """Ответ, основанный на прежней версии состояния, не применяется (R-108)."""
        c = self.c
        req = self.ask()
        c.apply(self.prop(c, op_set("traits", "changed", 1)))      # пока модель думала, личность изменилась
        v, state = c.state_version, c.state
        late = self.answer(req, [proposal([op("beliefs", "coffee", "set", "оператор любит кофе")])])
        res = c.receive_response(late)
        self.assertEqual((res.status, res.reason), ("stale", "version"))
        self.assertEqual((c.state, c.state_version), (state, v))
        self.assertNotIn("любит кофе", str(c.state))
        stale = c.store.find_event(key=f"resp:{req['request_id']}:stale")
        self.assertEqual((stale.type, stale.payload["reasons"], stale.changes_state), ("proposal.stale", ["version"], False))
        self.assertIsNotNone(c.store.find_event(key=f"resp:{req['request_id']}"), "ответ записан, хотя не применён")
        self.assertEqual(c.receive_response(late).status, "duplicate")
        self.assertEqual(self.types(c).count("proposal.stale"), 1)

    def test_epoch_alone_makes_answer_stale(self):
        """Версия состояния та же, но эпоха другая (после отката из копии): ответ устарел."""
        from iam.storage import Snapshot
        c = self.c
        req = self.ask()
        snap = c.store.latest_snapshot()
        c.store.put_snapshot(Snapshot(2, snap.state_version, snap.at_seq, snap.life_time_ms, snap.created_at, snap.state))
        c._reload()
        self.assertEqual((c.epoch, c.state_version), (2, snap.state_version))
        res = c.receive_response(self.answer(req, [proposal([op("traits", "a", "set", 1)])]))
        self.assertEqual((res.status, res.reason), ("stale", "version"))

    def test_other_epoch_is_stale_too(self):
        c = self.c
        req = self.ask()
        c.apply(self.prop(c, op_set("traits", "n", 1)))
        # запрос основан на эпохе 1; ответ с эхом другой эпохи — не опоздание, а неверное эхо
        res = c.receive_response(self.answer(req, [proposal([op("traits", "a", "set", 1)])], epoch=2))
        self.assertEqual(res.reason, "echo_mismatch")

    def test_non_state_events_do_not_expire_the_answer(self):
        c = self.c
        req = self.ask()
        c.record_event(EventSpec("input.arrived", "rx:tg:9"))         # приход сообщения версию не меняет
        res = c.receive_response(self.answer(req, [proposal([op("traits", "a", "set", 1)])]))
        self.assertEqual(res.status, "applied")

    def test_state_changing_event_expires_the_answer(self):
        c = self.c
        req = self.ask()
        c.apply(self.prop(c, op_set("traits", "n", 1), carrier=EventSpec("input.noticed", "ntc:inp_ABCDEFGH:N1")))
        self.assertEqual(c.receive_response(self.answer(req, [proposal([op("traits", "a", "set", 1)])])).status, "stale")

    def test_session_changes_make_answer_stale(self):
        c = self.c
        req = self.ask()
        c.pause_session()
        res = c.receive_response(self.answer(req, [proposal([op("traits", "a", "set", 1)])]))
        self.assertEqual(res.status, "stale")
        self.assertEqual(c.store.find_event(key=f"resp:{req['request_id']}:stale").payload["reasons"],
                         ["version", "session"], "в паузе ответ только фиксируется, состояние не меняется")
        c.resume_session()
        req2 = self.ask()
        c.stop_session()
        c.start_session()
        res2 = c.receive_response(self.answer(req2, [proposal([op("traits", "a", "set", 1)])]))
        self.assertEqual(res2.status, "stale", "другой сеанс — тот же вывод")
        self.assertIn("session", c.store.find_event(key=f"resp:{req2['request_id']}:stale").payload["reasons"])

    def test_deadline(self):
        c = self.c
        req = self.ask(deadline=1000)
        self.time.advance_mono(1001)
        res = c.receive_response(self.answer(req, [proposal([op("traits", "a", "set", 1)])]))
        self.assertEqual((res.status, res.reason), ("stale", "deadline"))
        req2 = self.ask(deadline=1000)
        self.time.advance_mono(1000)
        self.assertEqual(c.receive_response(self.answer(req2, [proposal([op("traits", "a", "set", 1)])])).status, "applied",
                         "ровно в срок — ещё можно")

    def test_answer_after_restart_cannot_apply(self):
        c = self.c
        req = self.ask()
        c2 = self.reopen(c)
        res = c2.receive_response(self.answer(req, [proposal([op("traits", "a", "set", 1)])]))
        self.assertEqual(res.status, "stale")
        self.assertIn("deadline_unknown", c2.store.find_event(key=f"resp:{req['request_id']}:stale").payload["reasons"])


class RejectTest(ResponseCase):
    def test_bad_format_and_missing_echo(self):
        c = self.c
        req = self.ask()
        good = self.answer(req, [proposal([op("traits", "a", "set", 1)])])
        for name in ("request_id", "epoch", "state_version"):
            bad = dict(good)
            del bad[name]
            res = c.receive_response(bad)
            self.assertEqual((res.status, res.reason), ("rejected", "bad_format"), name)
        self.assertEqual(c.receive_response("не объект").reason, "bad_format")
        self.assertNotIn("a", c.state["traits"])
        self.assertEqual(self.types(c).count("proposal.received"), 0)
        self.assertEqual(c.receive_response(good).status, "applied", "настоящий ответ после мусора применяется")

    def test_garbage_that_is_not_json_is_still_refused_and_logged(self):
        res = self.c.receive_response({"request_id": {1, 2}})
        self.assertEqual((res.status, res.reason), ("rejected", "bad_format"))
        self.assertIn("system.error", self.types(self.c))

    def test_echo_must_repeat_the_request_not_just_the_current_state(self):
        """Ответ, эхо которого не совпадает с версией запроса, — ошибка провайдера, даже если эхо совпало
        с нынешней версией: контекст собирался на другой версии."""
        c = self.c
        req = self.ask()
        c.apply(self.prop(c, op_set("traits", "moved", 1)))
        liar = self.answer(req, [proposal([op("traits", "a", "set", 1)])], state_version=c.state_version)
        res = c.receive_response(liar)
        self.assertEqual((res.status, res.reason), ("rejected", "echo_mismatch"))
        self.assertNotIn("a", c.state["traits"])
        wrong_epoch = self.answer(self.ask(), [proposal([op("traits", "a", "set", 1)])], epoch=7)
        self.assertEqual(c.receive_response(wrong_epoch).reason, "echo_mismatch")

    def test_unknown_request(self):
        res = self.c.receive_response(self.answer({"request_id": "req_NEVERASKED", "epoch": 1, "state_version": 2}, []))
        self.assertEqual((res.status, res.reason), ("rejected", "unknown_request"))

    def test_status_not_ok_is_ignored(self):
        for status in ("refused", "error"):
            req = self.ask()
            res = self.c.receive_response(self.answer(req, [proposal([op("traits", "a", "set", 1)])], status=status))
            self.assertEqual((res.status, res.reason), ("ignored", status))
        self.assertNotIn("a", self.c.state["traits"])
        self.assertEqual(self.types(self.c).count("proposal.received"), 2, "ответ записан даже без применения")

    def test_tool_use_makes_answer_unusable(self):
        """R-117: ответ, в котором обнаружен вызов инструмента, ничего не меняет."""
        c = self.c
        req = self.ask()
        p = proposal([op("traits", "a", "set", 1)], pid="prop_TOOLUSE01")
        res = c.receive_response(self.answer(req, [p], tool_use_detected=True))
        self.assertEqual((res.status, res.reason, res.rejected), ("rejected", "tool_use_detected",
                                                                 {"prop_TOOLUSE01": "tool_use_detected"}))
        self.assertNotIn("a", c.state["traits"])
        self.assertEqual(c.store.find_event(key="prop:prop_TOOLUSE01:reject").payload["reason"], "tool_use_detected")
        self.assertIn("system.error", self.types(c))

    def test_each_proposal_is_checked_separately(self):
        c = self.c
        req = self.ask()
        good = proposal([op("traits", "ok", "set", 1)], pid="prop_GOOD00001")
        overlap = proposal([op("traits", "ok.deeper", "set", 2)], pid="prop_OVERLAP01")
        ghost = proposal([op("traits", "ghost", "remove")], pid="prop_GHOST0001")
        message = proposal([], kind="message", pid="prop_MESSAGE01",
                           body={"capability_id": "cap_ABCDEFGH", "to": "оператор", "text": "привет"})
        lost = proposal([op("traits", "lost", "set", 1)], cites=["evt_DOESNOTEXIST"], pid="prop_LOST00001")
        memory_cite = proposal([op("traits", "lost2", "set", 1)], cites=["mem_DOESNOTEXIST"], pid="prop_LOST00002")
        protected = proposal([op("world_time", "session_state", "set", "stopped")], pid="prop_PROTECT01")
        memory_op = proposal([op("memory", "mem_AAAAAAAA", "set", {})], pid="prop_MEMORY001")
        res = c.receive_response(self.answer(req, [good, overlap, ghost, message, lost, memory_cite, protected, memory_op]))
        self.assertEqual((res.status, res.accepted), ("partial", ("prop_GOOD00001",)))
        self.assertEqual(res.rejected, {
            "prop_OVERLAP01": "conflicts_with_accepted", "prop_GHOST0001": "path_missing",
            "prop_MESSAGE01": "kind_not_supported", "prop_LOST00001": "unknown_cite", "prop_LOST00002": "unknown_cite",
            "prop_PROTECT01": "protected_field", "prop_MEMORY001": "memory_via_proposal"})
        self.assertEqual(c.state["traits"]["ok"], 1)
        self.assertEqual(c.session_state, "running")
        for pid, why in res.rejected.items():
            ev = c.store.find_event(key=f"prop:{pid}:reject")
            self.assertEqual((ev.type, ev.payload["reason"], ev.changes_state), ("proposal.rejected", why, False))
        self.assertEqual(self.types(c).count("proposal.accepted"), 1)
        self.assertEqual(verify(c.store), [])

    def test_all_rejected_means_no_transition(self):
        c = self.c
        req = self.ask()
        v = c.state_version
        res = c.receive_response(self.answer(req, [proposal([op("traits", "ghost", "remove")]), proposal([], kind="no_action",
                                                                                                 body={"reason": "отдых"})]))
        self.assertEqual((res.status, res.reason, res.transition), ("rejected", "no_proposal_accepted", None))
        self.assertEqual(c.state_version, v)

    def test_empty_proposal_list(self):
        req = self.ask()
        res = self.c.receive_response(self.answer(req, []))
        self.assertEqual((res.status, res.reason), ("rejected", "no_proposal_accepted"))

    def test_duplicate_proposal_ids_in_one_answer_are_refused(self):
        c = self.c
        req = self.ask()
        twin = [proposal([op("traits", "a", "set", 1)], pid="prop_TWIN00001"),
                proposal([op("traits", "b", "set", 1)], pid="prop_TWIN00001")]
        res = c.receive_response(self.answer(req, twin))
        self.assertEqual((res.status, res.reason), ("rejected", "duplicate_proposal_id"))
        self.assertEqual(c.state["traits"].keys(), {"curious"})
        self.assertEqual(c.receive_response(self.answer(req, twin)).status, "duplicate", "повтор уже не падает")

    def test_reused_proposal_id_cannot_be_accepted_twice(self):
        c = self.c
        first = c.receive_response(self.answer(self.ask(), [proposal([op("traits", "a", "set", 1)], pid="prop_REUSED001")]))
        self.assertEqual(first.status, "applied")
        second = c.receive_response(self.answer(self.ask(), [proposal([op("traits", "b", "set", 1)], pid="prop_REUSED001")]))
        self.assertEqual((second.status, second.reason), ("rejected", "key_taken"))
        self.assertEqual(second.rejected, {"prop_REUSED001": "key_taken"})
        self.assertEqual(c.store.find_event(key="prop:prop_REUSED001:reject").payload["reason"], "key_taken")
        self.assertNotIn("b", c.state["traits"])


if __name__ == "__main__":
    unittest.main()
