"""Мотивация A по доступному опыту: сигналы, а не алгоритм выбора занятия."""
from iam.domain import Op
from iam.state import KindDecision, Violation
from iam.world import identifier, text, fingerprint


class Motivation:
    def __init__(self, memory, intentions, *, off=False):
        self.memory, self.ctl, self.intentions, self.off = memory, memory.ctl, intentions, off
        self.ctl.register_kind('memory_write', self.proposal)
        self.ctl.add_validator('motivation_sources', self.validate)

    def proposal(self, pr, ctx):
        draft = pr['body']['draft']
        content = draft['content']
        if 'motivation' not in content:
            return self.memory.experience.proposal(pr, ctx)
        try:
            spec, sources = content['motivation'], pr['cites']
            rows = {r.memory_id: r for r in self.memory.records()} if not self.memory.off and not self.off else {}
            visible = {item['ref'] for item in ctx.request['context']}
            if (not isinstance(spec, dict) or set(spec) != {'topic', 'interest', 'need'}
                    or not identifier(spec['topic']) or not text(content.get('text'), 512)
                    or draft['kind'] != 'semantic' or draft['origin'] != 'own_hypothesis'
                    or any(type(spec[key]) not in (int, float) or not 0 <= spec[key] <= 1 for key in ('interest', 'need'))
                    or not sources or any(ref not in rows or ref not in visible for ref in sources)
                    or not any(rows[ref].origin == 'observation' for ref in sources)):
                raise ValueError('Нужен опыт и допустимая гипотеза')
            rec = self.memory.prepare(kind='semantic', origin='own_hypothesis', text=content['text'],
                                      sources=sources, layer='interpretation',
                                      content={'motivation': dict(spec, kind='self_estimate', skill_verified=False)})
            pointer = {'memory_id': rec.memory_id, 'version': rec.version}
            rec.content['motivation_pointer'] = {'topic': spec['topic'], **pointer}
            return KindDecision((Op('memory', rec.memory_id, 'add', rec.to_dict(), True),
                                 Op('preferences', 's22.' + spec['topic'], 'set', pointer, True)))
        except (ValueError, TypeError, KeyError):
            return KindDecision(violation=Violation('motivation_invalid', 'Гипотеза мотивации требует видимого опыта'))

    @staticmethod
    def validate(check):
        allowed = [change['after']['content'].get('motivation_pointer') for change in check.changes
                   if change['group'] == 'memory']
        return ['Мотивация требует атомарной записи опыта' for change in check.changes
                if change['group'] == 'preferences' and change['path'].split('.')[0] == 's22'
                and not any(item and change['path'] == 's22.' + item['topic']
                            and change['after'] == {k: item[k] for k in ('memory_id', 'version')} for item in allowed)]

    def signals(self, records):
        if self.off or self.memory.off:
            return {'enabled': False}
        available = {r.memory_id: r for r in self.memory.records()}
        visible = {r.memory_id: r for r in records if r.memory_id in available and available[r.memory_id].version == r.version}
        interests = {}
        for topic, pointer in self.ctl.state['preferences'].get('s22', {}).items():
            row = available.get(pointer.get('memory_id'))
            if row is not None and row.version == pointer.get('version'):
                interests[topic] = {**row.content['motivation'], 'source': row.memory_id}
        history = {}
        seen = set()
        for row in visible.values():
            if row.origin != 'observation' or row.version != 1 or row.content.get('outcome') not in ('confirmed', 'failed', 'unknown'):
                continue
            aid = row.content['action_id']
            if aid in seen:
                continue
            seen.add(aid)
            action = self.ctl.state['world_time']['world'].get('actions', {}).get(aid)
            if action is None:
                continue
            params = action.get('params', {})
            target = params.get('project_id', params.get('material_id', params.get('object_id')))
            if target is not None:
                event = self.ctl.find_event(event_id=row.sources[0])
                history.setdefault(target, []).append((event.seq, aid, row.memory_id, action))
        items = []
        for target, values in sorted(history.items()):
            values.sort()
            latest = values[-1][3]
            failures, confirmed = 0, []
            for _, _, mid, action in values:
                if action['status'] == 'confirmed':
                    confirmed.append(fingerprint(action['outcome']))
                    failures = 0
                elif action['status'] == 'failed':
                    failures += 1
                else:
                    failures = 0
            repeats = sum(left == right for left, right in zip(confirmed, confirmed[1:]))
            estimate = interests.get(target)
            # Запись нового текста не даёт прибавки. Неудачи/точные повторы снижают прежнюю оценку.
            adjusted = max(0, estimate['interest'] - .15 * repeats - .25 * failures) if estimate else None
            items.append({'target': target, 'sources': [v[2] for v in values], 'last_outcome': latest['status'],
                          'exact_repeats': repeats, 'consecutive_failures': failures, 'interest': adjusted,
                          'text_novelty': None if not confirmed else (0 if repeats else 1),
                          'meaningful_progress': None, 'skill': None})
        goals = self.intentions.visible()
        assessed = [g for g in goals.values() if g.get('completion', {}).get('kind') == 'model_assessed']
        return {'enabled': True, 'mechanism': 'voluntary_goals', 'selects_action': False,
                'interests': interests, 'items': items,
                'unfinished': [key for key, g in goals.items() if g['status'] in ('active', 'deferred', 'waiting_opportunity')],
                'needs': {key: value['need'] for key, value in interests.items()},
                'skill_measurement': {'rubric': 'подтверждённое достижение исходного критерия по отдельной модельной оценке',
                                      'completed': len(assessed), 'total_goals': len(goals),
                                      'verified_human_skill': False},
                'rule': 'Оценки интереса/потребностей — гипотезы по опыту. Можно выбрать любое занятие или отдых; создавать цель необязательно.'}


__all__ = ['Motivation']
