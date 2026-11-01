"""S19: версии, происхождение оснований, ослабление и операторское восстановление."""
from dataclasses import replace
import json

from iam.domain import Link, MemoryRecord, Op, Transition, new_id
from iam.state import EventSpec, Proposal

WEEK_MS = 604800000
POLICY = 'memory_independence'


class MemoryLifecycle:
    def _current(self, mid):
        r = self.ctl.store.get_memory(mid)
        if mid not in {x.memory_id for x in self.records()}:
            raise ValueError('Запись недоступна обычной памяти')
        return r

    def _move_memory(self, records, event_type, reason, **payload):
        if self.off:
            raise ValueError('Память отключена')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Нужна причина изменения памяти')
        tid, eid = new_id('trn'), new_id('evt')
        ops = tuple(Op('memory', r.memory_id, how, r.to_dict(), True) for how, r in records)
        t = Transition(tid, self.ctl.persona_id, self.ctl.epoch, self.ctl.state_version, ops, reason.strip(), (eid,))
        prefix = {'memory.generalized':'gen', 'memory.revised':'rev', 'memory.faded':'fad', 'memory.linked':'lnk'}[event_type]
        key = f'{prefix}:{tid}' + (':0' if prefix == 'lnk' else '')
        result = self.ctl.apply(Proposal(t, carrier=EventSpec(event_type, key, payload, event_id=eid)))
        if result.applied:
            self._sensory.clear()
        return result

    def _version(self, old, reason, **changes):
        history = (*old.history, {'version': old.version + 1, 'at_seq': self.ctl.store.last_seq() + 1,
                                 'change': reason})
        if changes.get('content', old.content).get('text') != old.text:
            history[-1]['previous_text'] = old.text
            history[-1]['previous_sources'] = list(old.sources)
        return replace(old, version=old.version + 1, history=history, **changes)

    def generalize(self, memory_ids, text, *, reason='обобщение эпизодов'):
        rows = [self._current(mid) for mid in dict.fromkeys(memory_ids)]
        if not rows or any(r.kind != 'episodic' for r in rows):
            raise ValueError('Нужны доступные эпизоды')
        r = self.prepare(kind='semantic', origin='own_hypothesis', text=text,
                         sources=tuple(r.memory_id for r in rows), layer='interpretation')
        return r, self._move_memory([('add', r)], 'memory.generalized', reason)

    def revise(self, mid, text, *, sources, reason):
        old = self._current(mid)
        if not text.strip() or not sources:
            raise ValueError('Исправление требует текста и оснований')
        # История остаётся оператору, модель получает только текущий текст и текущие основания.
        versions = {l.target: old.content.get('source_versions', {}).get(l.target, 1) for l in old.links if l.type == 'same_source'}
        content = {**old.content, 'text': text, 'source_versions': {**versions, **self._source_versions(sources)}}
        r = self._version(old, reason, content=content, sources=tuple(sources))
        return self._move_memory([('set', r)], 'memory.revised', reason,
                                 supersedes={'memory_id': mid, 'version': old.version})

    def link(self, mid, target, *, type='same_source', reason='связь памяти'):
        old = self._current(mid)
        if type == 'same_source' and target == mid:
            raise ValueError('Связь на саму себя недопустима')
        versions = {**old.content.get('source_versions', {}), **self._source_versions((target,) if type == 'same_source' else ())}
        r = self._version(old, reason, links=(*old.links, Link(type, target)), content={**old.content, 'source_versions': versions})
        return self._move_memory([('set', r)], 'memory.linked', reason)

    def fade(self, mid, *, availability=0, detail=0, reason='ослабление памяти'):
        old = self._current(mid)
        for value, previous in ((availability, old.availability), (detail, old.detail)):
            if type(value) not in (int, float) or not 0 <= value <= (1 if previous is None else previous):
                raise ValueError('Забывание может только снижать доступность и детали')
        r = self._version(old, reason, availability=availability, detail=detail)
        return self._move_memory([('set', r)], 'memory.faded', reason)

    def fade_due(self):
        if self.off or self.ctl.session_state != 'running':
            return []
        results = []
        for r in list(self.records()):
            if (self.ctl.life_time_ms - r.created_life_ms >= WEEK_MS * (4 if r.kind == 'autobiographical' else 1)
                    and r.memory_id in {x.memory_id for x in self.records()}):
                results.append(self.fade(r.memory_id, reason='истёк срок доступности во времени жизни'))
        return results

    def _source_versions(self, sources):
        return {ref: r.version for ref in sources if ref.startswith('mem_') and (r := self._source(ref)) is not None}

    @staticmethod
    def _restored_snapshot(r):
        saved = r.content.get('restored_support', {})
        return saved if (r.restored_by_operator and r.sources == (saved.get('archive_event'),)
                         and not any(l.type == 'same_source' for l in r.links)) else None

    def support(self, memory_ids):
        """Считаются группы первичных внешних оснований, а не число текстов/версий."""
        rows = {r.memory_id: r for r in self.records()}
        groups, own, seen = set(), set(), set()
        pending = list(memory_ids)
        while pending:
            mid = pending.pop()
            if mid in seen or mid not in rows:
                continue
            seen.add(mid)
            r = rows[mid]
            if saved := self._restored_snapshot(r):
                groups.update(saved['groups'])
                own.update(saved['self_groups'])
                continue
            parents = [s for s in r.sources if s in rows]
            parents += [l.target for l in r.links if l.type == 'same_source' and l.target in rows]
            if parents:
                pending.extend(parents)
            elif r.origin in ('observation', 'stated_by_other') and r.claim_kind != 'statement_made':
                groups.add(r.independence_group or 'unknown:' + r.sources[0])
            else:
                own.add('self:' + (r.independence_group or r.sources[0]))
        confirmed = set(self.ctl.state['world_time'].get('world', {}).get(POLICY, []))
        return {'independent_support': len(groups & confirmed), 'confirmed_groups': sorted(groups & confirmed),
                'unknown_groups': sorted(groups - confirmed), 'self_groups': sorted(own)}

    def confirm_independence(self, groups, *, reason):
        groups = sorted(set(groups))
        known = self.support([r.memory_id for r in self.records()])
        if len(groups) < 2 or not set(groups) <= set(known['confirmed_groups'] + known['unknown_groups']):
            raise ValueError('Нужны хотя бы две известные внешние группы')
        return self.ctl.intervene('technical_fix', reason,
                (Op('world_time', 'world.' + POLICY, 'set', groups, True),), target='memory:independence')

    def _blocked(self):
        rows = self.ctl.readable_memories()
        available = {r.memory_id for r in self.records()}
        ids = {r.memory_id for r in rows if r.memory_id not in available}
        texts = {r.text for r in rows if r.memory_id in ids}
        texts.update(h['previous_text'] for r in rows for h in r.history if 'previous_text' in h)
        return ids | texts

    def _safe_item(self, value):
        text = json.dumps(value, ensure_ascii=False)
        return not any(json.dumps(marker, ensure_ascii=False)[1:-1] in text for marker in self._blocked())

    def decision_state(self):
        """Выбор внимания не читает кеш цели/оценки с забытым основанием."""
        self.fade_due()
        state = self.ctl.state
        state = {group: {key: value for key, value in fields.items() if self._safe_item(value)}
                 for group, fields in state.items()}
        # Единственный подключённый потребитель текста состояния — внимание к намерению.
        # Кеш без прослеживаемых оснований не имеет права возвращать забытые детали.
        state['intentions'] = {key: value for key, value in state['intentions'].items() if self._intention_available(value)}
        return self.experience.state(state)

    def _intention_available(self, value):
        if (not isinstance(value, dict) or not isinstance(value.get('text'), str)
                or not isinstance(value.get('sources'), list) or not value['sources']
                or not isinstance(value.get('source_versions', {}), dict)):
            return False
        for ref in value['sources']:
            if not isinstance(ref, str):
                return False
            source = self._source(ref)
            version = value.get('source_versions', {}).get(ref, 1)
            if not isinstance(source, MemoryRecord) or type(version) is not int or version != source.version:
                return False
        return True

    def _archive_version(self, mid, version):
        for event in self.ctl.store.events():
            for change in event.payload.get('changes', []):
                raw = change.get('after')
                if change.get('group') == 'memory' and raw and raw['memory_id'] == mid and raw['version'] == version:
                    return event, MemoryRecord.from_dict(raw)
        raise ValueError('Версия записи не найдена в техническом архиве')

    def _archive_support(self, original):
        """Только явное восстановление: сохранить корни архивного графа, не новый пересказ."""
        groups, own, seen, pending = set(), set(), set(), [original]
        while pending:
            r = pending.pop()
            key = (r.memory_id, r.version)
            if key in seen:
                continue
            seen.add(key)
            if saved := self._restored_snapshot(r):
                groups.update(saved['groups'])
                own.update(saved['self_groups'])
                continue
            parents = set(s for s in r.sources if s.startswith('mem_'))
            parents.update(l.target for l in r.links if l.type == 'same_source' and l.target.startswith('mem_'))
            if parents:
                for ref in parents:
                    _, parent = self._archive_version(ref, r.content.get('source_versions', {}).get(ref, 1))
                    pending.append(parent)
            elif r.origin in ('observation', 'stated_by_other') and r.claim_kind != 'statement_made':
                groups.add(r.independence_group or 'unknown:' + r.sources[0])
            else:
                own.add('self:' + (r.independence_group or r.sources[0]))
        return {'groups': sorted(groups), 'self_groups': sorted(own)}

    def restore_from_archive(self, mid, version, *, reason):
        if self.off:
            raise ValueError('Память отключена')
        event, old = self._archive_version(mid, version)
        if old.origin == 'simulation_event' or old.persona_id != self.ctl.persona_id:
            raise ValueError('Чужая или экспериментальная биография недопустима')
        r = replace(old, memory_id=new_id('mem'), layer='personal', version=1, availability=0.6,
                    restored_by_operator=True, created_at=self.ctl.clock.calendar_now(),
                    created_life_ms=self.ctl.life_time_ms, sources=(event.event_id,), links=(), history=(),
                    content={**old.content, 'archive_ref': {'memory_id': mid, 'version': version},
                             'restored_support': {**self._archive_support(old), 'archive_event': event.event_id}})
        return r, self.ctl.restore_memory(r, reason=reason)

    def _valid_restore(self, r, check):
        if check.actor != 'operator' or check.carrier_type != 'operator.intervention':
            return False
        ref = r.content.get('archive_ref', {})
        if not isinstance(ref, dict):
            return False
        try:
            event, old = self._archive_version(ref.get('memory_id'), ref.get('version'))
        except ValueError:
            return False
        expected = replace(old, memory_id=r.memory_id, layer='personal', version=1, availability=0.6,
                           restored_by_operator=True, created_at=r.created_at, created_life_ms=r.created_life_ms,
                           sources=(event.event_id,), links=(), history=r.history,
                           content={**old.content, 'archive_ref': {'memory_id': old.memory_id, 'version': old.version},
                                    'restored_support': {**self._archive_support(old), 'archive_event': event.event_id}})
        return r == expected and old.origin != 'simulation_event' and old.persona_id == self.ctl.persona_id

    def _validate_lifecycle(self, check):
        errors = []
        before = check.before['world_time'].get('world', {}).get(POLICY)
        after = check.after['world_time'].get('world', {}).get(POLICY)
        if before != after and (check.actor != 'operator' or check.carrier_type != 'operator.intervention'):
            errors.append('Независимость подтверждает только оператор отдельным вмешательством')
        for change in check.changes:
            if change['group'] != 'memory':
                continue
            r = MemoryRecord.from_dict(change['after'])
            if r.restored_by_operator and not change['before'] and not self._valid_restore(r, check):
                errors.append('Восстановление требует точной архивной версии и вмешательства оператора')
            old = change['before']
            if old:
                old = MemoryRecord.from_dict(old)
                if check.carrier_type == 'memory.faded' and (r.availability is None or r.detail is None
                        or r.availability > (1 if old.availability is None else old.availability)
                        or r.detail > (1 if old.detail is None else old.detail)):
                    errors.append('Забывание не повышает доступность или детали')
                if r.version != old.version + 1 or r.history[:len(old.history)] != old.history:
                    errors.append('Пересмотр требует следующей версии и сохранения истории')
                if r.content.get('restored_support') != old.content.get('restored_support'):
                    errors.append('Корневые основания восстановления неизменяемы')
                if (not self._available(old) or r.origin != old.origin or r.independence_group != old.independence_group
                        or r.restored_by_operator != old.restored_by_operator):
                    errors.append('Нельзя незаметно восстановить или сменить происхождение записи')
            if any(l.target == r.memory_id for l in r.links) or r.memory_id in r.sources:
                errors.append('Циклическое основание памяти')
        return errors
