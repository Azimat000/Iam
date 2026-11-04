"""Представление о себе из текущего состояния и доступных записей."""
PARTS = {
    "now": "Какая я сейчас",
    "history": "Что со мной происходило",
    "values_view": "Что для меня важно",
    "relations_view": "Какие у меня отношения",
    "skills_view": "Что я умею",
    "fallibility": "В чём могу ошибаться",
}
LEVELS = {"observation": "Наблюдение", "hypothesis": "Гипотеза", "stable_change": "Изменение черты"}


class SelfModel:
    def __init__(self, memory, capabilities):
        self.memory, self.capabilities, self.ctl = memory, capabilities, memory.ctl

    def records(self):
        if self.memory.off:
            return {}
        blocked = self.memory._blocked()
        return {row.memory_id: row for row in self.memory.records()
                if self.memory._safe_item(row.to_dict(), blocked=blocked)}

    def history(self, rows):
        entries = self.ctl.state["selfmodel"].get("reflections", {})
        if not entries:
            return []
        blocked = self.memory._blocked()
        return [entry for entry in entries.values()
                if entry["memory_id"] in rows and rows[entry["memory_id"]].version == 1
                and all(ref in rows and rows[ref].version == version for ref, version in entry["refs"].items())
                and self.memory._safe_item(entry, blocked=blocked)]

    def view(self):
        state = self.memory.experience.state(self.ctl.state)
        rows = self.records()
        records = sorted(rows.values(), key=lambda row: (row.created_life_ms, row.memory_id), reverse=True)

        def cited(row):
            return {"ref": row.memory_id, "text": row.text[:400]}

        affect = state["affect"]
        emotion = self.ctl.state["affect"].get("emotion")
        now = {key: affect[key] for key in ("emotion", "load", "attention") if key in affect}
        now["traits"] = state["traits"].get("traits", [])
        now["refs"] = [emotion["memory_id"]] if isinstance(emotion, dict) and emotion.get("memory_id") in rows else []
        # Начальные черты и ценности имеют общий источник — создание личности.
        creation = next((event.event_id for event in self.ctl.store.events() if event.type == "persona.created"), None)
        trait_sources = [trait.get("source") for trait in now["traits"] if isinstance(trait, dict)]
        if creation and any(not str(ref).startswith("mem_") for ref in trait_sources):
            now["refs"].append(creation)
        now["refs"].extend(ref for ref in trait_sources if isinstance(ref, str) and ref in rows)
        values = {"values": state["traits"].get("values", []), "likes": state["preferences"].get("likes", [])}
        if creation and values["values"]:
            values["refs"] = [creation]
        people = self.memory.overview()["people"] if hasattr(self.memory, "overview") else {}
        relations = {key: {**person, "history": [ref for ref in person["history"] if ref in rows][-8:]}
                     for key, person in people.items()}
        actions = state["world_time"].get("world", {}).get("actions", {})
        biography = []
        for row in records:
            if row.origin != "observation" or row.content.get("experience") != "live":
                continue
            if row.kind == "autobiographical":
                biography.append(cited(row))
            elif row.kind == "episodic" and row.content.get("outcome") == "confirmed":
                action = actions.get(row.content.get("action_id"), {})
                scope = action.get("receipt", {}).get("scope")
                labels = {"artifact": "Создана работа.", "received_text": "Прочитан материал.",
                          "world_state": "Подтверждено изменение в личном пространстве."}
                biography.append({"ref": row.memory_id, "text": labels.get(scope, "Подтверждён результат действия.")})
        skills = {
            "capabilities": [{"id": cap.capability_id, "name": cap.name, "status": cap.status}
                             for cap in self.capabilities.list()],
            "methods": [cited(row) for row in records if row.kind == "procedural" and "procedure" in row.content][:8],
        }
        parts = {
            "now": now,
            "history": biography[:8],
            "values_view": values,
            "relations_view": relations,
            "skills_view": skills,
            "fallibility": [cited(row) for row in records if row.content.get("outcome") in ("failed", "unknown")][:8],
        }
        blocked = self.memory._blocked()
        # Значение черты остаётся, но забытая ссылка не публикуется.
        now["traits"] = [{**trait, "source": None} if isinstance(trait, dict)
                         and str(trait.get("source", "")).startswith("mem_") and trait["source"] not in rows else trait
                         for trait in now["traits"]]
        return {"parts": {key: value if self.memory._safe_item(value, blocked=blocked) else None
                          for key, value in parts.items()},
                "history": sorted(self.history(rows), key=lambda entry: entry["state_version"], reverse=True)}

    def roots(self, row, actions):
        """Подтверждения разных объектов, без повторных редакций и копий."""
        if (row.origin != "observation" or row.content.get("experience") != "live"
                or row.content.get("outcome") != "confirmed"):
            return {}
        action_id = row.content.get("action_id")
        receipt = actions.get(action_id, {}).get("receipt", {})
        ref = receipt.get("event_id")
        if ref not in row.sources:
            return {}
        event = self.memory._source(ref)
        if getattr(event, "type", None) != "action.confirmed" or event.payload.get("action_id") != action_id:
            return {}
        return {f"{event.payload['capability_id']}:{receipt['target']}":
                (receipt["fingerprint"], event.life_time_ms)}
