"""Социальная история строится из прочитанного и ещё доступного опыта."""

import json

from iam.domain.contacts import contact_id
from iam.memory.window import text_window
from iam.perception import Perception
from iam.world.cycle import WorldMemory


class SocialMemory(WorldMemory):
    @staticmethod
    def contact(arrival):
        sender = str(arrival.payload.get("sender", "unknown"))
        channel = arrival.payload.get("channel", "")
        return contact_id(channel, sender)

    def _read_arrivals(self):
        events = self.ctl.events_after(0)
        arrivals = {Perception.input_id(event): event for event in events if event.type == "input.arrived"}
        return {event.event_id: arrivals.get(event.payload.get("input_id"))
                for event in events if event.type == "input.read"}

    def history(self):
        profiles = {"creator": {"role": "operator_interlocutor", "history": []}}
        if self.off:
            return profiles
        reads = self._read_arrivals()
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
        return [*context, *self.dialogue(trigger)]

    def _turns(self):
        """Доступные реплики с порядком и авторством; тело берётся только из памяти."""
        if self.off:
            return []
        rows = self.records()
        arrivals = self._read_arrivals()
        people = {ref: person for person, profile in self.history().items() for ref in profile['history']}
        candidates = []
        for row in rows:
            if row.version != 1 or row.kind != 'episodic' or len(row.sources) != 1:
                continue
            if row.memory_id in people:
                direction, person, text, outcome = 'incoming', people[row.memory_id], row.text, 'read'
                received_seq = arrivals[row.sources[0]].seq
            elif self.observation_scope(row) == 'outgoing_words_only':
                communication = row.content.get('communication', {})
                direction, person = 'outgoing', communication.get('person', 'creator')
                # Старые исходы содержат лишь «Telegram: sent»: не читаем тело из world.actions/журнала.
                text, outcome = communication.get('text'), row.content.get('outcome')
                received_seq = None
            else:
                continue
            source = self._source(row.sources[0])
            if source is None or not hasattr(source, 'seq'):
                continue
            candidates.append((source.seq, row, direction, person, text, outcome, received_seq))
        return sorted(candidates, key=lambda item: item[0])

    def recall(self, query, *, limit=5, current=None):
        ranked = super().recall(query, limit=limit, current=current)
        if not query.strip() or not ranked:
            return ranked
        turns = self._turns()
        adjacent = {}
        for i, turn in enumerate(turns):
            # Ближайшая реплика другой стороны помогает вспомнить ответ и его автора.
            j = i + (1 if turn[2] == 'incoming' else -1)
            if 0 <= j < len(turns) and turns[j][3] == turn[3] and turns[j][2] != turn[2]:
                adjacent[turn[1].memory_id] = turns[j][1]
        # Найденные по запросу основания нельзя вытеснять нерелевантными соседями.
        selected = {row.memory_id: row for row in ranked}
        for row in ranked:
            neighbor = adjacent.get(row.memory_id)
            if neighbor is not None and current not in neighbor.sources and row.text[:self.text_limit] != query:
                selected.setdefault(neighbor.memory_id, neighbor)
        return list(selected.values())[:min(limit, self.working_limit)]

    def dialogue(self, trigger=None):
        """Ограниченная хронология доступных реплик; транспортный архив не восстанавливается."""
        candidates = self._turns()
        result, budget = [], 2 * self.text_limit
        latest = {direction:max((item[0] for item in candidates if item[2] == direction), default=None)
                  for direction in ('incoming', 'outgoing')}
        for seq, row, direction, person, text, outcome, received_seq in sorted(candidates, key=lambda item: item[0], reverse=True)[:6]:
            metadata = {'kind': 'dialogue_turn', 'version': row.version, 'seq': seq, 'direction': direction, 'person': person,
                        'received_seq': received_seq, 'outcome': outcome, 'content_available': text is not None,
                        'latest_in_direction': seq == latest[direction],
                        'current_read': direction == 'incoming' and getattr(trigger, 'type', None) == 'input.read'
                                        and getattr(trigger, 'event_id', None) == row.sources[0],
                        'age_life_ms': max(0, self.ctl.life_time_ms - row.created_life_ms)}
            packed = text_window(metadata, text or '', min(800, self.text_limit, budget))
            if packed is None:
                break
            packet = json.loads(packed)
            # Это явно обозначенный краткий обзор, а не подмена страниц самой mem_.
            packet['excerpt'] = packet.pop('text_window')
            packed = json.dumps(packet, ensure_ascii=False)
            budget -= len(packed)
            result.append({'ref': row.memory_id, 'origin': row.origin, 'layer': 'state', 'text': packed})
        return list(reversed(result))
