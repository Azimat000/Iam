"""Память S18 без импорта экспериментальных биографий и доступа к архиву."""
import copy
from dataclasses import replace
import json
import math

from iam.domain import MemoryRecord, Op, Source, Transition, new_id
from iam.state import EventSpec, KindDecision, Proposal, Violation
from iam.storage import NotFound
from .lifecycle import MemoryLifecycle
from .window import text_window
from .search import relevance, diverse, historical_candidates

LAYERS = ("personal", "interpretation")
OUTCOMES = {"action.confirmed": "confirmed", "message.out_sent": "confirmed",
            "action.failed": "failed", "action.unknown_outcome": "unknown", "message.out_unknown": "unknown"}


class PersistentMemory(MemoryLifecycle):
    """Единственная биография из controller; off отключает чтение и запоминание опыта."""
    def __init__(self, controller, *, off=False, working_limit=8, sensory_limit=8,
                 sensory_ttl_ms=60000, text_limit=2000):
        if any(type(n) is not int or n < 1 for n in (working_limit, sensory_limit, sensory_ttl_ms, text_limit)):
            raise ValueError("Лимиты памяти должны быть положительными целыми числами")
        self.ctl, self.off = controller, off
        self.working_limit, self.sensory_limit = working_limit, sensory_limit
        self.sensory_ttl_ms, self.text_limit = sensory_ttl_ms, text_limit
        self._sensory = []
        controller.register_kind("memory_write", self._proposal)
        controller.add_validator("memory_provenance", self._validate_changes)
        controller.add_validator("memory_lifecycle", self._validate_lifecycle)
        from iam.emotion.integration import Experience
        self.experience = Experience(self)

    def _available(self, r):
        return (r.persona_id == self.ctl.persona_id and r.layer in LAYERS
                and r.origin != "simulation_event" and (r.availability is None or r.availability > 0)
                and (r.detail is None or r.detail >= 1)
                and not r.content.get("superseded_by") and not any(h.get("superseded_by") for h in r.history))

    def records(self):
        """Текущие записи без чтения журнала/архива; используется также страницей памяти."""
        rows = {r.memory_id: r for r in self.ctl.readable_memories() if self._available(r)}
        # Удаление до неподвижной точки исключает всю цепочку зависимых кешей.
        while True:
            hidden = {mid for mid, r in rows.items()
                      if any(ref.startswith('mem_') and (ref not in rows or
                             r.content.get('source_versions', {}).get(ref, 1) != rows[ref].version)
                             for ref in r.sources)
                      or any(l.type == 'same_source' and l.target.startswith('mem_') and
                             (l.target not in rows or r.content.get('source_versions', {}).get(l.target, 1)
                              != rows[l.target].version) for l in r.links)}
            if not hidden:
                return list(rows.values())
            rows = {mid: r for mid, r in rows.items() if mid not in hidden}

    def retrieval_query(self, trigger, fallback=''):
        """Содержание повода берётся только из ещё доступной личной записи прочитанного."""
        if self.off:
            return ''
        if trigger.type != 'input.read':
            return fallback
        rows = [r for r in self.records() if r.origin == 'stated_by_other'
                and trigger.event_id in r.sources]
        packed = self._window(max(rows, key=lambda r: (r.created_life_ms, r.memory_id))) if rows else None
        return json.loads(packed)['text'] if packed is not None else ''

    def _window(self, r, start=0, *, recall_role=None, metadata=None, limit=None):
        extra = metadata or {}
        metadata = {'source': r.source.to_dict(), 'source_details': r.content.get('source_details', []), 'created_at': r.created_at,
                    'created_life_ms': r.created_life_ms, 'experience': r.content.get('experience', 'unknown'),
                    'outcome': r.content.get('outcome'), 'sources': list(r.sources), 'kind': r.kind,
                    'version': r.version, 'observation_scope': self.observation_scope(r)}
        metadata.update(extra)
        if recall_role is not None:
            metadata["recall_role"] = recall_role
        limit = min(self.text_limit, 2000 if recall_role is not None else self.text_limit,
                    limit if limit is not None else self.text_limit)
        return text_window(metadata, r.text, limit, start)

    def retrieve(self, query, *, limit=5):
        return self._retrieve(query, limit=limit, natural=False)

    def recall(self, query, *, limit=5, current=None):
        """Ассоциативное извлечение по прочитанной реплике; точный retrieve сохраняет контракт."""
        return self._retrieve(query, limit=limit, natural=True, current=current)

    def _retrieve(self, query, *, limit, natural, current=None):
        if type(limit) is not int or limit < 0:
            raise ValueError("Неверный предел извлечения")
        if self.off or limit == 0:
            return []
        self.fade_due()
        available = {r.memory_id: r for r in self.records()}
        matches = (relevance(query, available.values()) if natural else
                   {r.memory_id: 1.0 for r in self.ctl.store.search_memory(query, layers=LAYERS, limit=-1)
                    if r.memory_id in available})
        direct = set(matches)
        associated = {link.target for mid in direct for link in available[mid].links if link.target in available}
        candidates = direct | associated if query.strip() else set(available)
        # Текущая прочитанная запись уже отдельно включается в context().
        # Она не должна отнимать место у воспоминаний, нужных для ответа.
        if natural and current is not None:
            candidates = {mid for mid in candidates if current not in available[mid].sources}
        now = self.ctl.life_time_ms
        def score(mid):
            r = available[mid]
            significance = r.content.get("importance", 0.5)
            if type(significance) not in (int, float) or not math.isfinite(significance):
                significance = 0.5
            freshness = 1 / (1 + max(0, now - r.created_life_ms) / 60000)
            return (-matches.get(mid, 0) if natural else 0,
                    -(2 * (mid in direct) + freshness + min(1, max(0, significance))), -r.created_life_ms, mid)
        ranked = sorted(candidates, key=score)
        count = min(limit, self.working_limit)
        selected = diverse(ranked, matches, available, count) if natural and matches else ranked[:count]
        return [available[mid] for mid in selected]

    def context(self, trigger, first, records):
        """Буфер хранит только безопасное уведомление цикла, не payload входящего."""
        now = self.ctl.life_time_ms
        self._sensory = [(at, item) for at, item in self._sensory if now - at < self.sensory_ttl_ms and self._safe_item(item)]
        first = first if self._safe_item(first) else {**first, 'text': 'Наступил плановый момент.'}
        self._sensory.append((now, dict(first, text=first['text'][:self.text_limit])))
        self._sensory = self._sensory[-self.sensory_limit:]
        result = [dict(first, text=first['text'][:self.text_limit])]
        direct_ids = set()
        if not self.off:
            current = {r.memory_id: r for r in self.records()}
            page = getattr(trigger, 'payload', {}) if getattr(trigger, 'type', '') == 'activity.completed' else {}
            opened = current.get(page.get('memory_id'))
            if opened is not None and opened.version != page.get('memory_version'):
                opened = None
            direct = sorted((r for r in current.values() if getattr(trigger, 'event_id', None) in r.sources),
                            key=lambda r: (-r.created_life_ms, r.memory_id))[:1]
            if opened is not None:
                direct = [opened]
            direct_ids = {r.memory_id for r in direct}
            selected = {r.memory_id: r for r in [*direct, *records]}
            for saved in list(selected.values())[:self.working_limit - 1]:
                r = current.get(saved.memory_id)
                if r is not None:
                    start = page['offset'] if opened is not None and r.memory_id == opened.memory_id else 0
                    text = self._window(r, start)
                    if text is None:
                        continue
                    result.append({'ref': r.memory_id, 'origin': r.origin, 'layer': r.layer,
                                   'text': text})
        interpreted = self.experience.context()
        if not self.off and (len(result) > 1 or interpreted):
            result[0]['text'] = (result[0]['text'] + self.experience.instructions)[:self.text_limit]
        direct_context = [item for item in result[1:] if item['ref'] in direct_ids]
        ordered = [result[0], *direct_context, *interpreted, *result[1:]]
        unique = {}
        for item in ordered:
            unique.setdefault(item['ref'], item)
        base = list(unique.values())[:self.working_limit]
        return [*base, *self._historical_context(trigger, records, base)]

    def _historical_context(self, trigger, records, base):
        # Дополнительное явно помеченное окно: максимум 2 × min(text_limit, 2000).
        # Исходный рабочий буфер и все его факты остаются неизменными.
        if self.off or getattr(trigger, 'type', None) != 'input.read' or not records:
            return []
        query = self.retrieval_query(trigger)
        if not query.strip():
            return []
        available = {r.memory_id: r for r in self.records()}
        shown = {item['ref'] for item in base}
        candidates = historical_candidates(query, available, min(len(records), self.working_limit),
                                           current=trigger.event_id)
        result = []
        for mid in candidates:
            if mid in shown:
                continue
            row = available[mid]
            text = self._window(row, recall_role='historical_episode')
            if text is not None:
                item = {'ref': mid, 'origin': row.origin, 'layer': row.layer, 'text': text}
                if self._safe_item(item):
                    result.append(item)
        return result

    @property
    def sensory(self):
        now = self.ctl.life_time_ms
        return [copy.deepcopy(item) for at, item in self._sensory if now - at < self.sensory_ttl_ms and self._safe_item(item)]

    def _source(self, ref):
        if ref.startswith("mem_"):
            try:
                rec = self.ctl.store.get_memory(ref)
            except NotFound:
                return None
            return rec if any(r.memory_id == ref for r in self.records()) else None
        event = self.ctl.find_event(event_id=ref)
        if event is None or event.persona_id != self.ctl.persona_id or event.type == "input.arrived":
            return None
        # Навигация по уже существующей памяти не становится независимым опытом.
        # Иначе evt_ страницы переживёт забывание исходной mem_ и отмоет её происхождение.
        if event.type == 'activity.completed' and 'memory_id' in event.payload:
            return None
        if event.type in (*OUTCOMES, 'input.read', 'activity.completed') and any(
                (not self._available(r) and ref in r.sources) or any(ref in h.get('previous_sources', []) for h in r.history)
                for r in self.ctl.readable_memories()):
            return None
        return event

    def _validate_changes(self, check):
        errors = []
        for change in check.changes:
            if change['group'] != 'memory':
                continue
            r = MemoryRecord.from_dict(change['after'])
            old = change['before']
            if check.carrier_type == 'memory.faded' and old:
                expected = dict(old, version=r.version, availability=r.availability, detail=r.detail, history=list(r.to_dict()['history']))
                if (expected == r.to_dict() and r.availability is not None and r.detail is not None
                        and r.availability <= old.get('availability', 1) and r.detail <= old.get('detail', 1)):
                    continue
            if r.restored_by_operator and self._valid_restore(r, check):
                continue
            sources = [self._source(s) for s in r.sources]
            if any(s is None for s in sources) or any(self._source(l.target) is None for l in r.links):
                errors.append("Источник или связь недоступны")
            if r.created_life_ms > self.ctl.life_time_ms:
                errors.append("Время памяти из будущего")
            if r.origin == 'stated_by_other' and r.claim_kind != 'fact_claim':
                errors.append("Чужое утверждение должно быть помечено fact_claim")
            if r.kind == 'autobiographical' and (r.origin != 'observation' or r.content.get('experience') != 'live'):
                errors.append('Автобиография требует собственного наблюдения')
            if r.origin == 'observation' and not any(getattr(s, 'type', '') in (*OUTCOMES, 'input.read', 'activity.completed') for s in sources):
                errors.append('Нет события наблюдения')
            if r.content.get('experience') == 'live' and r.origin in ('model_knowledge', 'imagination'):
                errors.append('Знание модели и воображение не являются прожитым опытом')
            if r.content.get('experience') == 'live' and any(isinstance(s, MemoryRecord) and s.content.get('experience', 'unknown') != 'live' for s in sources):
                errors.append('Подготовленный опыт нельзя присвоить')
            status = r.content.get('outcome')
            if status is not None and not any(OUTCOMES.get(getattr(s, 'type', None)) == status
                                          and s.payload.get('action_id') == r.content.get('action_id')
                                          and bool(r.content.get('action_id')) for s in sources):
                errors.append("Исход не подтверждён источником")
            if r.content.get('experience', 'unknown') not in ('prepared', 'live', 'unknown'):
                errors.append("Неизвестное происхождение опыта")
        return errors

    def prepare(self, *, kind, origin, text, sources, links=(), layer='personal', content=None,
                claim_kind=None, independence_group=None):
        """Заготовка ядра; источник и часы задаются ядром, происхождение явно указано вызывающим."""
        data = dict(content or {}, text=text)
        data.setdefault('experience', 'unknown')
        data['source_versions'] = self._source_versions((*sources, *(l.target for l in links if l.type == 'same_source')))
        data['source_details'] = [s.source.to_dict() for ref in sources if (s := self._source(ref)) is not None]
        return MemoryRecord(new_id('mem'), self.ctl.persona_id, layer, kind, origin, data,
                            Source('core', 'memory'), self.ctl.clock.calendar_now(), self.ctl.life_time_ms,
                            tuple(sources), tuple(links), history=({'version': 1, 'at_seq': self.ctl.store.last_seq() + 1, 'change':'создана'},), claim_kind=claim_kind, independence_group=independence_group)

    def write(self, record, *, base=None):
        """Ядро предлагает версионную запись; контроллер атомарно проверяет и применяет её."""
        record = replace(record, history=record.history or ({'version': record.version,
                         'at_seq': self.ctl.store.last_seq() + 1, 'change': 'создана'},))
        epoch, version = base or (self.ctl.epoch, self.ctl.state_version)
        causes = tuple(s for s in record.sources if s.startswith('evt_'))
        # При ссылке на память переход основан на последнем подтверждённом снимке.
        if not causes:
            causes = (self.ctl.store.get_event(self.ctl.store.latest_snapshot().at_seq).event_id,)
        t = Transition(new_id('trn'), self.ctl.persona_id, epoch, version,
                       (Op('memory', record.memory_id, 'add', record.to_dict(), True),), 'запоминание', causes)
        return self.ctl.apply(Proposal(t, carrier=EventSpec('memory.recorded', f'mem:{t.transition_id}:0')))

    def _proposal(self, proposal, ctx):
        if self.off:
            return KindDecision(violation=Violation('memory_off', 'Память отключена'))
        draft, cites = proposal['body']['draft'], proposal['cites']
        visible = {c['ref'] for c in ctx.request['context']}
        if not cites or any(c not in visible or self._source(c) is None for c in cites):
            return KindDecision(violation=Violation('memory_source_hidden', 'Основания не видны в контексте'))
        if draft['kind'] == 'autobiographical':
            return KindDecision(violation=Violation('memory_biography', 'Модель не сочиняет биографию'))
        if draft['origin'] not in ('own_hypothesis', 'imagination', 'model_knowledge', 'model_explanation'):
            return KindDecision(violation=Violation('memory_origin', 'Модель не подтверждает внешние факты'))
        text = draft['content'].get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > self.text_limit:
            return KindDecision(violation=Violation('memory_text', 'Недопустимый текст памяти'))
        rec = self.prepare(kind=draft['kind'], origin=draft['origin'], text=text, sources=cites,
                           layer='interpretation', content={'experience': 'unknown'})
        return KindDecision(ops=(Op('memory', rec.memory_id, 'add', rec.to_dict(), True),))

    def describe(self, action, outcome):
        if self.off:
            return None
        label = {'confirmed': 'Подтверждён успех', 'failed': 'Подтверждена неудача', 'unknown': 'Исход неизвестен'}[outcome.status]
        description = f'{label}: {action.kind} {action.capability_id}. {outcome.summary}'
        if action.kind == 'message':
            description += f' Мои слова собеседнику: {action.text}'
        return description

    def observation_scope(self, record):
        """Подтверждение действия не подтверждает содержание написанного/прочитанного."""
        if record.origin == 'stated_by_other':
            return 'received_statement_only'
        if record.origin != 'observation' or record.version != 1:
            return None
        action = self.ctl.state['world_time'].get('world', {}).get('actions', {}).get(record.content.get('action_id'), {})
        if action.get('kind') == 'message':
            return 'outgoing_words_only'
        scope = action.get('receipt', {}).get('scope')
        return {'artifact': 'created_text_only', 'received_text': 'read_text_only'}.get(scope)

    def remember_outcome(self, action, outcome, event):
        text = self.describe(action, outcome)
        if text is None:
            return None
        content = {'outcome': outcome.status, 'experience': 'live', 'action_id': action.action_id}
        if action.kind == 'message':
            content['communication'] = {'person': action.to, 'text': action.text}
        rec = self.prepare(kind='episodic', origin='observation', text=text, sources=(event.event_id,), content=content)
        return rec, self.write(rec)
