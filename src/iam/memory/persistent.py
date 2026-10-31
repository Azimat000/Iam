"""Память S18 без импорта экспериментальных биографий и доступа к архиву."""
import copy
from dataclasses import replace
import json
import math

from iam.domain import MemoryRecord, Op, Source, Transition, new_id
from iam.state import EventSpec, KindDecision, Proposal, Violation
from iam.storage import NotFound

LAYERS = ("personal", "interpretation")
OUTCOMES = {"action.confirmed": "confirmed", "message.out_sent": "confirmed",
            "action.failed": "failed", "action.unknown_outcome": "unknown", "message.out_unknown": "unknown"}


class PersistentMemory:
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

    def _available(self, r):
        return (r.persona_id == self.ctl.persona_id and r.layer in LAYERS
                and r.origin != "simulation_event" and (r.availability is None or r.availability > 0)
                and not r.content.get("superseded_by") and not any(h.get("superseded_by") for h in r.history))

    def records(self):
        """Текущие записи без чтения журнала/архива; используется также страницей памяти."""
        return [r for r in self.ctl.readable_memories() if self._available(r)]

    def retrieve(self, query, *, limit=5):
        if type(limit) is not int or limit < 0:
            raise ValueError("Неверный предел извлечения")
        if self.off or limit == 0:
            return []
        available = {r.memory_id: r for r in self.records()}
        hits = self.ctl.store.search_memory(query, layers=LAYERS, limit=-1)
        direct = {r.memory_id for r in hits if r.memory_id in available}
        associated = {link.target for mid in direct for link in available[mid].links if link.target in available}
        candidates = direct | associated if query.strip() else set(available)
        now = self.ctl.life_time_ms
        def score(mid):
            r = available[mid]
            significance = r.content.get("importance", 0.5)
            if type(significance) not in (int, float) or not math.isfinite(significance):
                significance = 0.5
            freshness = 1 / (1 + max(0, now - r.created_life_ms) / 60000)
            return (-(2 * (mid in direct) + freshness + min(1, max(0, significance))), -r.created_life_ms, mid)
        return [available[mid] for mid in sorted(candidates, key=score)[:min(limit, self.working_limit)]]

    def context(self, trigger, first, records):
        """Буфер хранит только безопасное уведомление цикла, не payload входящего."""
        now = self.ctl.life_time_ms
        self._sensory = [(at, item) for at, item in self._sensory if now - at < self.sensory_ttl_ms]
        self._sensory.append((now, dict(first, text=first['text'][:self.text_limit])))
        self._sensory = self._sensory[-self.sensory_limit:]
        result = [dict(first, text=first['text'][:self.text_limit])]
        if not self.off:
            for r in records[:self.working_limit - 1]:
                if self._available(r):
                    metadata = {'source': r.source.to_dict(), 'source_details': r.content.get('source_details', []), 'created_at': r.created_at,
                                'created_life_ms': r.created_life_ms, 'experience': r.content.get('experience', 'unknown'),
                                'outcome': r.content.get('outcome'), 'sources': list(r.sources), 'kind': r.kind,
                                'version': r.version}
                    metadata['text'] = ''
                    overhead = len(json.dumps(metadata, ensure_ascii=False))
                    if overhead > self.text_limit:
                        continue
                    metadata['text'] = r.text[:(self.text_limit - overhead) // 6]
                    text = json.dumps(metadata, ensure_ascii=False)
                    result.append({'ref': r.memory_id, 'origin': r.origin, 'layer': r.layer,
                                   'text': text[:self.text_limit]})
        return result

    @property
    def sensory(self):
        now = self.ctl.life_time_ms
        return [copy.deepcopy(item) for at, item in self._sensory if now - at < self.sensory_ttl_ms]

    def _source(self, ref):
        if ref.startswith("mem_"):
            try:
                rec = self.ctl.store.get_memory(ref)
            except NotFound:
                return None
            return rec if self._available(rec) else None
        event = self.ctl.find_event(event_id=ref)
        if event is None or event.persona_id != self.ctl.persona_id or event.type == "input.arrived":
            return None
        return event

    def _validate_changes(self, check):
        errors = []
        for change in check.changes:
            if change['group'] != 'memory':
                continue
            r = MemoryRecord.from_dict(change['after'])
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
        return f'{label}: {action.kind} {action.capability_id}. {outcome.summary}'

    def remember_outcome(self, action, outcome, event):
        text = self.describe(action, outcome)
        if text is None:
            return None
        rec = self.prepare(kind='episodic', origin='observation', text=text, sources=(event.event_id,),
                           content={'outcome': outcome.status, 'experience': 'live', 'action_id': action.action_id})
        return rec, self.write(rec)
