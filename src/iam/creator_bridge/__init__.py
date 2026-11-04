"""Учёт просьб и явных решений создателя."""

import hashlib

from iam.domain import Op
from iam.interfaces import Delivery

from iam.capabilities import TELEGRAM_ID

PATH = "world.creator_requests"


def records(state):
    return state["world_time"].get("world", {}).get("creator_requests", {})


def request_id(proposal_id):
    return "crq_" + proposal_id.removeprefix("prop_")


def delivery_id(crq_id):
    return "act_" + hashlib.sha256(crq_id.encode()).hexdigest()[:16]


def event_key(key, crq_id, repeated=False):
    return f"crq:{key}.{crq_id}" if repeated else f"crq:{key}"


class Bridge:
    def __init__(self, transport, registry, recipient=None):
        self.transport, self.registry, self.recipient = transport, registry, recipient

    def poll_replies(self):
        return self.transport.poll_replies()

    def submit(self, request):
        reason = self.registry.execution_violation(TELEGRAM_ID, {"to": str(self.recipient)})
        if reason:
            return Delivery(False, reason)
        return self.transport.submit(request)


class Requests:
    def __init__(self, controller):
        self.ctl = controller

    @staticmethod
    def telegram_replies(events):
        arrivals = {event.event_id: event for event in events if event.type == "input.arrived"}
        reads = {event.event_id: event.payload.get("input_id") for event in events if event.type == "input.read"}
        replies = {}
        for event in events:
            if event.type == "creator.reply":
                arrival = arrivals.get(event.payload.get("arrival_event"))
                if arrival is not None and arrival.payload.get("input_id") in {
                    reads[cause] for cause in event.causes if cause in reads
                }:
                    replies.setdefault(event.payload["crq_id"], []).append({
                        "text": arrival.payload["text"], "event_id": event.event_id})
        return replies

    def respond(self, key, crq_id, decision, answer):
        item = records(self.ctl.state).get(key, {})
        if not crq_id or item.get("crq_id") != crq_id:
            raise ValueError("Просьба изменилась. Обновите страницу.")
        status = item.get("status")
        change = None
        if decision in ("accept", "decline") and status in ("accepted", "open"):
            change = {"status": "answered", "answer": answer,
                      "outcome": "waiting" if decision == "accept" else "declined"}
        elif decision == "record_result" and status == "answered" and item.get("outcome") == "waiting":
            change = {"outcome": "operator_reported", "result": answer}
        elif decision == "circumstances" and status in ("open", "answered"):
            change = {"status": "retry_allowed", "circumstances": answer}
        if change is None:
            raise ValueError("Этот ответ уже сохранён или не подходит к состоянию просьбы.")
        result = self.ctl.intervene("technical_fix", answer, (
            Op("world_time", f"{PATH}.{key}", "set", {**item, **change}, True),), target=crq_id)
        if not result.applied:
            raise ValueError("Ответ не сохранён. Проверьте журнал.")
        return result
