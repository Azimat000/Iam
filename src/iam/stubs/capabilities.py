"""StubCapabilities — реестр возможностей в памяти. Менять права вправе только оператор: в настоящей системе
`register`/`set_status` — вмешательства оператора (S23); личность и цикл их не вызывают."""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from iam.interfaces import Capability


class StubCapabilities:
    def __init__(self, capabilities: Iterable[Capability] = ()):
        self._caps: dict[str, Capability] = {}
        for c in capabilities:
            self.register(c)

    def register(self, capability: Capability) -> None:
        self._caps[capability.capability_id] = capability

    def set_status(self, capability_id: str, status: str) -> None:
        self._caps[capability_id] = replace(self._caps[capability_id], status=status)

    def list(self) -> list[Capability]:
        return list(self._caps.values())

    def get(self, capability_id: str) -> Capability | None:
        return self._caps.get(capability_id)
