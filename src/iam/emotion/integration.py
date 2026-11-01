"""Атомарные интерпретации доступной памяти через существующий memory_write."""
import json
import math
import re

from iam.domain import MemoryRecord, Op
from iam.state import KindDecision, Violation
from iam.beliefs import describe
from . import appraise, current


def number(value, low=0, high=1):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) is not None


class Experience:
    instructions = (' Интерпретацию можно предложить как memory_write с origin=own_hypothesis, '
        'cites доступных mem_ и content.text (до512 знаков). Для kind=emotional_associative '
        'content.appraisal={valence:-1..1,arousal:0..1,relevance:0..1,regulation:0..1}; '
        'для kind=semantic content.belief={belief_id,agreement:-1..1,confidence:0..1} или '
        'content.social={person,closeness?:0..1,trust_as_source?:0..1,readiness?:0..1,topic?}. '
        'Оценки добровольны; первичный источник первым, прошлые эмоции — явными cites. '
        'Согласие, близость, доверие и готовность меняются раздельно. Чувство не даёт прав.')
    def __init__(self, memory):
        self.memory, self.ctl = memory, memory.ctl
        self.ctl.register_kind('memory_write', self.proposal)
        self.ctl.add_validator('interpretation_state', self.validate)

    def _records(self):
        return {r.memory_id: r for r in self.memory.records()} if not self.memory.off else {}

    def _record(self, pointer, rows):
        if not isinstance(pointer, dict):
            return None
        ref = pointer.get('memory_id')
        row = rows.get(ref) if isinstance(ref, str) else None
        return row if row is not None and row.version == pointer.get('version') else None

    def state(self, state):
        """Ни забытый источник, ни прежняя версия не проходят через кеш состояния."""
        rows = self._records()
        emotion = self._record(state['affect'].get('emotion'), rows)
        state['affect'].pop('emotion', None)
        if emotion is not None and 'emotion' in emotion.content:
            state['affect']['emotion'] = current(emotion.content['emotion'], self.ctl.life_time_ms)
        state['beliefs'] = {key: value for key, value in state['beliefs'].items()
                            if self._record(value, rows) is not None}
        raw = self.ctl.state
        for name, person in state['relations'].items():
            if isinstance(person, dict):
                person.pop('s21_sources', None)
                pointers = raw['relations'].get(name, {}).get('s21_sources', {})
                for key, pointer in pointers.items():
                    if self._record(pointer, rows) is None:
                        person.pop(key, None)
        prefs = state['preferences']
        prefs.pop('s21_readiness_sources', None)
        pointers = raw['preferences'].get('s21_readiness_sources', {})
        for key, pointer in pointers.items():
            if self._record(pointer, rows) is None:
                prefs.get('change_readiness', {}).pop(key, None)
        return state

    def context(self):
        rows = self._records()
        result = []
        raw = self.ctl.state
        pointer = raw['affect'].get('emotion')
        r = self._record(pointer, rows)
        if r is not None and 'emotion' in r.content:
            result.append({'ref': r.memory_id, 'origin': r.origin, 'layer': 'interpretation',
                           'text': json.dumps({'emotion': current(r.content['emotion'], self.ctl.life_time_ms),
                                              'sources': list(r.sources)}, ensure_ascii=False)})
        for value in list(raw['beliefs'].values())[-4:]:
            r = self._record(value, rows)
            if r is not None and r.content.get('interpretation', {}).get('kind') == 'belief':
                result.append({'ref': r.memory_id, 'origin': r.origin, 'layer': 'interpretation',
                               'text': json.dumps(describe(r, self.memory), ensure_ascii=False)})
        return [item for item in result if len(item['text']) <= self.memory.text_limit]

    @staticmethod
    def protected(group, path):
        parts = path.split('.')
        return (group == 'beliefs' or group == 'affect' and parts[0] == 'emotion'
                or group == 'relations' and (len(parts) < 2 or parts[1] in ('closeness', 'trust_as_source', 's21_sources'))
                or group == 'preferences' and parts[0] in ('change_readiness', 's21_readiness_sources'))

    def validate(self, check):
        allowed = []
        for change in check.changes:
            if change['group'] == 'memory':
                allowed += change['after']['content'].get('state_ops', [])
        errors = []
        for change in check.changes:
            if self.protected(change['group'], change['path']):
                if not any(op['group'] == change['group'] and op['path'] == change['path']
                           and op['value'] == change['after'] for op in allowed):
                    errors.append('Оценка требует атомарной интерпретации доступного опыта')
        return errors

    def proposal(self, proposal, ctx):
        content = proposal['body']['draft']['content']
        keys = [key for key in ('appraisal', 'belief', 'social') if key in content]
        if not keys:
            return self.memory._proposal(proposal, ctx)
        try:
            return self._proposal(proposal, ctx, keys)
        except (ValueError, TypeError, KeyError):
            return KindDecision(violation=Violation('interpretation_invalid', 'Нужны доступные основания и корректная оценка'))

    def _proposal(self, proposal, ctx, keys):
        draft = proposal['body']['draft']
        content, sources = draft['content'], proposal['cites']
        text = content.get('text')
        rows = self._records()
        visible = {item['ref'] for item in ctx.request['context']}
        if (len(keys) != 1 or not sources or len(sources) > 8 or len(set(sources)) != len(sources)
                or any(ref not in visible or ref not in rows for ref in sources)
                or draft['origin'] != 'own_hypothesis' or not isinstance(text, str)
                or not text.strip() or len(text) > 512):
            raise ValueError('Недоступные основания или формат')
        kind, spec = keys[0], content[keys[0]]
        if not isinstance(spec, dict):
            raise ValueError('Неверная оценка')
        if kind == 'appraisal':
            prior = self._record(ctx.state['affect'].get('emotion'), rows)
            if prior is not None and prior.memory_id in visible and prior.memory_id not in sources:
                sources = [*sources, prior.memory_id]
        rec = self.memory.prepare(kind='emotional_associative' if kind == 'appraisal' else 'semantic',
                                  origin='own_hypothesis', text=text, sources=sources, layer='interpretation')
        if draft['kind'] != rec.kind:
            raise ValueError('Неверный вид памяти')
        old = None
        if kind == 'belief':
            if (set(spec) != {'belief_id', 'agreement', 'confidence'} or not identifier(spec['belief_id'])
                    or not number(spec['agreement'], -1) or not number(spec['confidence'])):
                raise ValueError('Неверное убеждение')
            old = self._record(ctx.state['beliefs'].get(spec['belief_id']), rows)
            if old is not None:
                if self._depends_on(sources, old.memory_id, rows):
                    raise ValueError('Убеждение не подтверждает само себя')
                rec = self.memory._version(old, proposal['rationale'], content=rec.content, sources=rec.sources)
        pointer = {'memory_id': rec.memory_id, 'version': rec.version}
        normalized = dict(spec, kind=kind)
        ops = []
        if kind == 'appraisal':
            if (set(spec) != {'valence', 'arousal', 'relevance', 'regulation'}
                    or not number(spec['valence'], -1)
                    or any(not number(spec[key]) for key in ('arousal', 'relevance', 'regulation'))):
                raise ValueError('Неверная эмоция')
            # Нельзя повторно назначать эмоцию тому же опыту; новая критика — новый эпизод.
            if any(r.content.get('interpretation', {}).get('kind') == 'appraisal'
                   and r.sources[0] == sources[0] for r in rows.values()):
                raise ValueError('Этот опыт уже оценён')
            state = self.memory.decision_state()
            previous = state['affect'].get('emotion')
            if prior is None or prior.memory_id not in sources:
                previous = None
            past = [current(rows[ref].content['emotion'], self.ctl.life_time_ms)['reaction']
                    for ref in proposal['cites'] if 'emotion' in rows[ref].content]
            emotion = appraise(spec, state, previous, sum(past) / len(past) if past else 0, self.ctl.life_time_ms)
            rec.content['emotion'] = emotion
            rec.content['importance'] = abs(emotion['reaction'])
            ops.append(Op('affect', 'emotion', 'set', pointer, True))
        elif kind == 'belief':
            value = {**pointer, 'claim': text, 'agreement': spec['agreement'], 'confidence': spec['confidence'],
                     'bases': list(sources)}
            ops.append(Op('beliefs', spec['belief_id'], 'set', value, True))
        else:
            fields = set(spec) - {'person', 'topic'}
            if (not identifier(spec.get('person')) or not fields
                    or not fields <= {'closeness', 'trust_as_source', 'readiness'}
                    or any(not number(spec[key]) for key in fields)
                    or ('readiness' in fields and not identifier(spec.get('topic')))
                    or ('topic' in spec and 'readiness' not in fields)):
                raise ValueError('Неверные социальные величины')
            person = spec['person']
            for key in sorted(fields):
                if key == 'readiness':
                    ops += [Op('preferences', 'change_readiness.' + spec['topic'], 'set', spec[key], True),
                            Op('preferences', 's21_readiness_sources.' + spec['topic'], 'set', pointer, True)]
                else:
                    ops += [Op('relations', person + '.' + key, 'set', spec[key], True),
                            Op('relations', person + '.s21_sources.' + key, 'set', pointer, True)]
        rec.content['interpretation'] = normalized
        rec.content['state_ops'] = [op.to_dict() for op in ops]
        return KindDecision(ops=(Op('memory', rec.memory_id, 'set' if old else 'add', rec.to_dict(), True), *ops))

    @staticmethod
    def _depends_on(sources, target, rows):
        seen, pending = set(), list(sources)
        while pending:
            ref = pending.pop()
            if ref == target:
                return True
            if ref not in seen and ref in rows:
                seen.add(ref)
                pending.extend(rows[ref].sources)
                pending.extend(link.target for link in rows[ref].links if link.type == 'same_source')
        return False


__all__ = ['Experience']
