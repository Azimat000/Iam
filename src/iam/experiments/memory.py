"""Общая минимальная память стенда: только доступный опыт текущего персонажа."""
from iam.stubs import StubMemory


class BenchMemory:
    def __init__(self, controller, *, off=False):
        self.controller, self.off = controller, off
        self.retrieved = []

    def retrieve(self, query, *, limit=5):
        if self.off:
            records = []
        else:
            # Память пишется и обычным переходом, и ответом модели: имя носителя не определяет её наличие.
            ids = dict.fromkeys(change["path"] for e in self.controller.events_after(0)
                                for change in e.payload.get("changes", []) if change["group"] == "memory")
            records = [self.controller.store.get_memory(mid) for mid in ids]
            records = StubMemory(records).retrieve(query, limit=limit)
        self.retrieved.extend(records)
        return records

    def describe(self, action, outcome):
        if self.off:
            return None
        return StubMemory().describe(action, outcome)
