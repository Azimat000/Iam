import hashlib
from unittest.mock import patch

from iam.cycle import Cycle
from iam.domain import Op, new_id
from iam.interfaces import ActionRequest, CreatorRequest
from iam.state import EventSpec, verify
from iam.stubs import (Reply, ScriptedProvider, StubCapabilities, StubCreatorBridge, StubMemory, ask, say)
from iam.interfaces import Capability
from iam.telegram import CreatorBridge, Sender

from .helpers import TelegramCase, USER, update

CAP = "cap_Telegram01"


class SenderTest(TelegramCase):
    def request(self, aid=None, text="привет"):
        if self.c.session_state == "paused":
            self.c.resume_session()
        elif self.c.session_state != "running":
            self.c.start_session()
        aid = aid or new_id("act")
        path = "world.actions." + aid
        pid = new_id("prop")
        accepted = self.c.apply(self.prop(self.c, Op("world_time", path, "set", {
            "status": "accepted", "kind": "message", "capability_id": CAP, "params": {},
            "proposal_id": pid, "cycle_id": new_id("cyc"), "to": str(USER), "text": text}, True),
            carrier=EventSpec("proposal.accepted", f"prop:{pid}:accept", {"proposal_id": pid})))
        self.assertTrue(accepted.applied, accepted)
        res = self.c.apply(self.prop(self.c, Op("world_time", path + ".status", "transition", "attempted", True),
            cause=accepted.events[0].event_id,
            carrier=EventSpec("message.out_requested", "out:" + aid, {"action_id": aid})))
        self.assertTrue(res.applied, res)
        return aid, res.events[0].seq

    def test_sent_confirmed_once_and_idempotent_across_restart(self):
        aid, seq = self.request()
        self.assertEqual(self.tx.send(aid, USER, "привет", requested_seq=seq), "sent")
        item = self.c.store.get_outbox(aid)
        self.assertEqual((item.status, item.external_id, item.requested_seq), ("sent", "1", seq))
        self.c = self.reopen(self.c)
        self.tx = Sender(self.c, self.client, [USER])
        self.assertEqual(self.tx.send(aid, USER, "привет", requested_seq=seq), "sent")
        self.assertEqual(len(self.api.messages), 1)
        with self.assertRaises(ValueError):
            self.tx.send(aid, USER, "другой текст", requested_seq=seq)

    def test_dropped_confirmation_uncertain_never_auto_retried(self):
        aid, seq = self.request()
        self.api.mode = "drop"
        self.assertEqual(self.tx.send(aid, USER, "привет", requested_seq=seq), "uncertain")
        self.api.mode = "ok"
        for _ in range(4):
            self.assertEqual(self.tx.send(aid, USER, "привет", requested_seq=seq), "uncertain")
        self.assertEqual(len(self.api.messages), 1)

    def test_error_classification(self):
        for mode, status in (("403", "failed"), ("429", "failed"), ("500", "uncertain"),
                             ("api_error", "failed"), ("bad_json", "uncertain"), ("redirect", "uncertain"),
                             ("truncated", "uncertain"), ("bad_status", "uncertain")):
            aid, seq = self.request()
            self.api.mode = mode
            self.assertEqual(self.tx.send(aid, USER, "привет", requested_seq=seq), status)
        self.api.mode = "ok"
        for result in ({}, [], {"message_id": True}, {"message_id": 1, "chat": []},
                       {"message_id": 1, "chat": {"id": 4, "type": "private"}},
                       {"message_id": 1, "chat": {"id": USER, "type": "group"}}):
            self.api.result = result
            aid, seq = self.request()
            self.assertEqual(self.tx.send(aid, USER, "привет", requested_seq=seq), "uncertain")

    def test_crash_after_attempt_record_before_status_is_uncertain_on_restart(self):
        aid, seq = self.request()
        with patch.object(self.client, "call", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.tx.send(aid, USER, "привет", requested_seq=seq)
        self.assertEqual(self.tx.status(aid), "pending")
        self.c = self.reopen(self.c)
        self.tx = Sender(self.c, self.client, [USER])
        self.assertEqual(self.tx.status(aid), "uncertain")
        self.assertEqual(self.tx.send(aid, USER, "привет", requested_seq=seq), "uncertain")
        self.assertEqual(self.api.messages, [])

    def test_same_process_reentry_after_crash_not_retried(self):
        aid, seq = self.request()
        with patch.object(self.client, "call", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.tx.send(aid, USER, "привет", requested_seq=seq)
        self.assertEqual(self.tx.send(aid, USER, "привет", requested_seq=seq), "uncertain")
        self.assertEqual(self.api.messages, [])

    def test_explicit_resend_bound_survives_restart_and_no_retry_chains(self):
        aid, seq = self.request()
        self.api.mode = "drop"
        self.tx.send(aid, USER, "привет", requested_seq=seq)
        retry, rseq = self.request()
        with self.assertRaises(ValueError):
            self.tx.send(retry, USER, "привет", requested_seq=rseq, retry_of=aid)
        self.assertEqual(self.tx.send(retry, USER, "привет", requested_seq=rseq,
                                     retry_of=aid, operator_approved=True), "uncertain")
        self.c = self.reopen(self.c)
        self.tx = Sender(self.c, self.client, [USER])
        third, tseq = self.request()
        for original in (aid, retry):
            with self.assertRaises(ValueError):
                self.tx.send(third, USER, "привет", requested_seq=tseq, retry_of=original, operator_approved=True)
        self.assertEqual(len(self.api.messages), 2)

    def test_reject_invalid_destination_body_and_request(self):
        aid, seq = self.request()
        for user in (True, "123", USER + 1):
            with self.assertRaises(ValueError):
                self.tx.send(aid, user, "привет", requested_seq=seq)
        for text in (None, "", "x" * 4097):
            with self.assertRaises(ValueError):
                self.tx.send(aid, USER, text, requested_seq=seq)
        for seq_, aid_, text in ((1, aid, "привет"), (seq, new_id("act"), "привет"), (seq, aid, "иной текст")):
            with self.assertRaises(ValueError):
                self.tx.send(aid_, USER, text, requested_seq=seq_)
        self.c.pause_session("operator")
        with self.assertRaises(ValueError):
            self.tx.send(aid, USER, "привет", requested_seq=seq)
        self.assertEqual(self.api.messages, [])

    def test_execute_adapter_and_full_cycle_events_memory(self):
        self.c.start_session()
        self.api.updates = [update()]
        self.rx.poll_once()
        provider = ScriptedProvider([Reply((say(CAP, str(USER), "проверка"),))])
        cycle = Cycle(self.c, provider, StubMemory(), self.tx, StubCapabilities([Capability(CAP, "Telegram")]),
                      StubCreatorBridge())
        trigger = next(e for e in self.c.store.events() if e.type == "input.arrived")
        report = cycle.run(trigger)
        self.assertEqual(report.actions[0].status, "confirmed", report)
        self.assertEqual(self.tx.status(report.actions[0].action_id), "sent")
        self.assertIn("message.out_sent", self.types(self.c))
        self.assertEqual(verify(self.c.store), [])
        self.assertIsNotNone(report.actions[0].memory_id)

    def test_uncertain_cycle_records_unknown_not_success_or_memory(self):
        self.c.start_session()
        self.api.updates = [update()]
        self.rx.poll_once()
        self.api.mode = "drop"
        cycle = Cycle(self.c, ScriptedProvider([Reply((say(CAP, str(USER), "проверка"),))]), StubMemory(), self.tx,
                      StubCapabilities([Capability(CAP, "Telegram")]), StubCreatorBridge())
        report = cycle.run(next(e for e in self.c.store.events() if e.type == "input.arrived"))
        self.assertEqual(report.actions[0].status, "unknown_outcome")
        self.assertIsNone(report.actions[0].memory_id)
        self.assertIn("message.out_unknown", self.types(self.c))
        self.assertNotIn("message.out_sent", self.types(self.c))

    def test_execute_reject_paths(self):
        for kind, cap, to in (("action", CAP, str(USER)), ("message", "cap_Unknown00", str(USER)),
                              ("message", CAP, None), ("message", CAP, "@operator"),
                              ("message", CAP, "١٢٣"), ("message", CAP, str(USER))):
            outcome = self.tx.execute(ActionRequest(new_id("act"), kind, cap, to=to, text="привет"))
            self.assertEqual(outcome.status, "failed")
        aid, seq = self.request()
        outcome = self.tx.execute(ActionRequest(aid, "message", CAP, to=str(USER), text="иной текст"))
        self.assertEqual(outcome.status, "failed")
        self.api.mode = "403"
        self.assertEqual(self.tx.execute(ActionRequest(aid, "message", CAP, to=str(USER), text="привет")).status, "failed")

    def test_creator_bridge_cycle_and_delivery_idempotency(self):
        self.c.start_session()
        self.api.updates = [update()]
        self.rx.poll_once()
        bridge = CreatorBridge(self.rx, self.tx, USER)
        cycle = Cycle(self.c, ScriptedProvider([Reply((ask("help", "помоги"),))]), StubMemory(), self.tx,
                      StubCapabilities(), bridge)
        cycle.run(next(e for e in self.c.store.events() if e.type == "input.arrived"))
        event = self.c.find_event(key="crq:help")
        req = CreatorRequest(event.payload["crq_id"], "help", "помоги")
        self.assertTrue(bridge.submit(req).delivered)
        self.assertEqual(len(self.api.messages), 1)
        self.assertFalse(bridge.submit(CreatorRequest(new_id("crq"), "missing", "помоги")).delivered)
        self.assertFalse(bridge.submit(CreatorRequest(new_id("crq"), "help", "иной текст")).delivered)
