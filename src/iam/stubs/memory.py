"""Заглушки памяти. Читают и подсказывают, что запомнить; записывает только StateController (R-105)."""
from __future__ import annotations

import re
from typing import Iterable

from iam.domain import MemoryRecord
from iam.interfaces import ActionOutcome, ActionRequest
from iam.storage import Store

READABLE_LAYERS = ("personal", "interpretation")   # архив обычному извлечению недоступен


def _describe(action: ActionRequest, outcome: ActionOutcome) -> str | None:
    if outcome.status != "confirmed":
        return None
    what = f"написала {action.to}: {action.text}" if action.kind == "message" else f"выполнила {action.capability_id}"
    return f"Я {what}" + (f" ({outcome.summary})" if outcome.summary else "")


class StubMemory:
    """Память в списке: поиск по словам запроса; пустой запрос — последние записи."""

    def __init__(self, records: Iterable[MemoryRecord] = ()):
        self.records = list(records)

    def retrieve(self, query: str, *, limit: int = 5) -> list[MemoryRecord]:
        usable = [r for r in self.records if r.layer in READABLE_LAYERS]
        words = set(re.findall(r"\w+", query.lower()))
        if not words:
            return usable[::-1][:limit]
        return [r for r in usable if words & set(re.findall(r"\w+", r.text.lower()))][:limit]

    def describe(self, action: ActionRequest, outcome: ActionOutcome) -> str | None:
        return _describe(action, outcome)


class StoreMemory:
    """Память поверх полнотекстового поиска хранилища (только чтение; слой archive не читается)."""

    def __init__(self, store: Store):
        self._store = store

    def retrieve(self, query: str, *, limit: int = 5) -> list[MemoryRecord]:
        return self._store.search_memory(query, layers=READABLE_LAYERS, limit=limit)

    def describe(self, action: ActionRequest, outcome: ActionOutcome) -> str | None:
        return _describe(action, outcome)
