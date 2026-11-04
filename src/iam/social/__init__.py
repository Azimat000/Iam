"""Социальная история строится из прочитанного и ещё доступного опыта."""

import hashlib
import json

from iam.perception import Perception
from iam.world.cycle import WorldMemory


class SocialMemory(WorldMemory):
    @staticmethod
    def contact(arrival):
        sender = str(arrival.payload.get("sender", "unknown"))
        channel = arrival.payload.get("channel", "")
        person = "creator" if channel == "telegram" else "person_" + hashlib.sha256(
            f"{channel}:{sender}".encode()).hexdigest()[:16]
        return person

    def history(self):
        profiles = {"creator": {"role": "operator_interlocutor", "history": []}}
        if self.off:
            return profiles
        events = self.ctl.events_after(0)
        arrivals = {Perception.input_id(event): event for event in events if event.type == "input.arrived"}
        reads = {event.event_id: arrivals.get(event.payload.get("input_id"))
                 for event in events if event.type == "input.read"}
        for row in sorted(self.records(), key=lambda row: (row.created_at, row.memory_id)):
            if row.origin != "stated_by_other" or row.kind != "episodic" or row.version != 1 or len(row.sources) != 1:
                continue
            arrival = reads.get(row.sources[0])
            if arrival is None:
                continue
            person = self.contact(arrival)
            profile = profiles.setdefault(person, {"role": "other", "history": []})
            profile["history"].append(row.memory_id)
            profile["last_contact_calendar"] = arrival.occurred_at
        return profiles

    def overview(self):
        state = self.experience.state(self.ctl.state)
        profiles = self.history()
        for person, profile in profiles.items():
            relation = state["relations"].get(person, {})
            profile["closeness"] = relation.get("closeness")
            profile["trust_as_source"] = relation.get("trust_as_source")
        return {"people": profiles,
                "agreement": {key: item["agreement"] for key, item in state["beliefs"].items()
                              if "agreement" in item},
                "readiness": state["preferences"].get("change_readiness", {})}

    def context(self, trigger, first, records):
        context = super().context(trigger, first, records)
        visible = {item["ref"] for item in context}
        relations = self.experience.state(self.ctl.state)["relations"]
        for person, profile in self.history().items():
            recent = [ref for ref in profile["history"] if ref in visible][-3:]
            if recent:
                values = {key: relations.get(person, {}).get(key)
                          for key in ("closeness", "trust_as_source")}
                context.append({"ref": recent[-1], "origin": "observation", "layer": "state",
                                "text": json.dumps({"person": person, "history": recent, **values},
                                                   ensure_ascii=False)})
        return context
