"""Общая минимальная память стенда: только доступный опыт текущего персонажа."""
from iam.stubs import StubMemory


class BenchMemory:
    def __init__(self, controller, *, off=False):
        self.controller, self.off = controller, off
        self.retrieved = []
        self.conversational = False

    def retrieve(self, query, *, limit=5):
        if self.off:
            records = []
        else:
            # Память пишется и обычным переходом, и ответом модели: имя носителя не определяет её наличие.
            ids = dict.fromkeys(change["path"] for e in self.controller.events_after(0)
                                for change in e.payload.get("changes", []) if change["group"] == "memory")
            records = [self.controller.store.get_memory(mid) for mid in ids]
            if self.conversational:
                # Старые версии остаются в базе и журнале, но обычное извлечение их не возвращает.
                records = [r for r in records if r.layer in ("personal", "interpretation")
                           and not r.content.get("superseded_by")]
                matching = StubMemory(records[::-1]).retrieve(query, limit=limit)
                selected = {r.memory_id for r in matching}
                records = (matching + [r for r in records[::-1] if r.memory_id not in selected])[:limit]
            else:
                records = StubMemory(records).retrieve(query, limit=limit)
        self.retrieved.extend(records)
        return records

    def describe(self, action, outcome):
        if self.off:
            return None
        return StubMemory().describe(action, outcome)
