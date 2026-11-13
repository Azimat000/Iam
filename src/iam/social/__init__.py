"""Социальная история строится из прочитанного и ещё доступного опыта."""

import json

from iam.domain.contacts import contact_id
from iam.memory.window import text_window
from iam.perception import Perception
from iam.world.cycle import WorldMemory
from .context import authorship, adjacent, neighbor_context
from .development import relationship_context, conversation_experience


class SocialMemory(WorldMemory):
    def _window(self, row, start=0, *, recall_role=None, metadata=None, limit=None):
        # Для работы/знания социальная хронология не нужна. Условия совпадают
        # с _turns; авторство по-прежнему подтверждается доступным прочтением.
        is_utterance = (row.kind == 'episodic' and row.version == 1 and len(row.sources) == 1
                        and (row.origin == 'stated_by_other'
                             or self.observation_scope(row) == 'outgoing_words_only'))
        turn = next((turn for turn in self._turns() if turn[1].memory_id == row.memory_id
                     and turn[1].version == row.version), None) if is_utterance else None
        extra = {**(authorship(turn) if turn is not None else {}), **(metadata or {})}
        return super()._window(row, start, recall_role=recall_role, metadata=extra, limit=limit)

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
        # Один согласованный набор метаданных на чистую сборку контекста; между
        # вызовами ничего не кешируем, чтобы забывание/пересмотр учитывались сразу.
        self._context_turns = self._turns()
        try:
            return self._social_context(trigger, first, records)
        finally:
            self._context_turns = None

    def _social_context(self, trigger, first, records):
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
                if getattr(trigger, 'type', None) == 'input.read':
                    current = next((row for row in self.records() if row.memory_id in recent
                                    and trigger.event_id in row.sources), None)
                    if current is not None:
                        rule = {
                            'kind': 'social_update_option', 'person': person, 'source': current.memory_id,
                            'rule': 'Если этот разговор изменил твоё отношение, можно вместе с обычным ответом '
                                    'предложить memory_write. Можно ничего не менять. Чужие слова не доказывают '
                                    'свою истинность. Близость, доверие и readiness независимы; readiness — '
                                    'готовность менять мнение или поведение по теме. Не разрешение и не согласие.',
                            'format': {'kind': 'memory_write', 'cites': [current.memory_id],
                                       'body': {'draft': {'kind': 'semantic', 'origin': 'own_hypothesis',
                                       'content': {'text': 'своя интерпретация до 512 знаков',
                                       'social': {'person': person}}}}},
                            'optional_fields': 'social: closeness, trust_as_source, readiness — свои оценки '
                                               'от 0 до 1; хотя бы одно поле. Для readiness нужен topic '
                                               '(1–64 латинские буквы, цифры, _ или -); без readiness topic не нужен.'}
                        detail = json.dumps(rule, ensure_ascii=False)
                        if len(detail) <= self.text_limit:
                            context.append({'ref': trigger.event_id, 'origin': 'model_knowledge',
                                            'layer': 'state', 'text': detail})
        if not self.off:
            interpreted = self.experience.state(self.ctl.state)
            rows = {row.memory_id: row for row in self.records()}
            pointers = self.ctl.state['preferences'].get('s21_readiness_sources', {})
            for topic, value in list(interpreted['preferences'].get('change_readiness', {}).items())[-4:]:
                row = self.experience._record(pointers.get(topic), rows)
                if row is None:
                    continue
                packet = {'kind': 'social_readiness', 'topic': topic, 'readiness': value,
                          'person': row.content.get('interpretation', {}).get('person'),
                          'source': row.memory_id, 'sources': list(row.sources),
                          'meaning': 'собственная готовность менять мнение или поведение по теме; не право и не согласие'}
                detail = json.dumps(packet, ensure_ascii=False)
                if len(detail) <= self.text_limit and self._safe_item(packet):
                    context.append({'ref': row.memory_id, 'origin': row.origin,
                                    'layer': 'interpretation', 'text': detail})
        dialogue = self.dialogue(trigger)
        context.extend(relationship_context(self, [*context, *dialogue], trigger))
        context.extend(conversation_experience(self, [*context, *dialogue]))
        return [*context, *neighbor_context(self, trigger, records, [*context, *dialogue]), *dialogue]

    def _turns(self):
        """Доступные реплики с порядком и авторством; тело берётся только из памяти."""
        if self.off:
            return []
        cached = getattr(self, '_context_turns', None)
        if cached is not None:
            return cached
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
        neighbors = adjacent(self, turns)
        # Найденные по запросу основания нельзя вытеснять нерелевантными соседями.
        selected = {row.memory_id: row for row in ranked}
        for row in ranked:
            neighbor = neighbors.get(row.memory_id)
            if neighbor is not None and current not in neighbor[1].sources:
                selected.setdefault(neighbor[1].memory_id, neighbor[1])
        return list(selected.values())[:min(limit, self.working_limit)]

    def dialogue(self, trigger=None):
        """Ограниченная хронология доступных реплик; транспортный архив не восстанавливается."""
        candidates = self._turns()
        result, budget = [], 2 * self.text_limit
        latest = {direction:max((item[0] for item in candidates if item[2] == direction), default=None)
                  for direction in ('incoming', 'outgoing')}
        for seq, row, direction, person, text, outcome, received_seq in sorted(candidates, key=lambda item: item[0], reverse=True)[:6]:
            metadata = {'speaker': 'interlocutor' if direction == 'incoming' else 'self',
                        'kind': 'dialogue_turn', 'version': row.version, 'seq': seq, 'direction': direction, 'person': person,
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
