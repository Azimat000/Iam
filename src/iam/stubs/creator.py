"""StubCreatorBridge — «почта оператора» в памяти: сохраняет просьбы, отдаёт заготовленные ответы."""
from __future__ import annotations

from iam.interfaces import CreatorRequest, Delivery, InboundMessage


class StubCreatorBridge:
    def __init__(self, *, deliver: bool = True):
        self.deliver = deliver
        self.submitted: list[CreatorRequest] = []
        self._inbound: list[InboundMessage] = []

    def submit(self, request: CreatorRequest) -> Delivery:
        self.submitted.append(request)
        return Delivery(self.deliver, "" if self.deliver else "канал недоступен")

    def say(self, text: str, external_id: str, sender: str = "operator") -> None:
        """Оператор написал личности (появится при следующем `poll_replies`)."""
        self._inbound.append(InboundMessage("stub", external_id, sender, text))

    def poll_replies(self) -> list[InboundMessage]:
        out, self._inbound = self._inbound, []
        return out
