"""Добровольные замыслы: исходный критерий неизменен, принятие не равно исполнению."""
import copy
import json

from iam.domain import Op, new_id
from iam.state import KindDecision, Violation
from iam.world import READ, WRITE, MOVE, identifier, text

OPERATIONS = {'read': READ, 'write': WRITE, 'move': MOVE}
OPEN_STATUSES = {'active', 'deferred', 'waiting_opportunity'}


class _DuplicateIntention(ValueError):
    pass


def _signature(goal):
    return (goal.get('project_id'), goal.get('operation'), goal.get('not_before_life_ms'),
            ' '.join(goal.get('text', '').split()), ' '.join(goal.get('criterion', '').split()))


class Intentions:
    def __init__(self, memory, world):
        self.memory, self.ctl, self.world = memory, memory.ctl, world
        self.reviews = {}
        self.grants = []
        self.ctl.register_kind('intention', self.proposal)
        self.ctl.add_validator('intentions_contract', self.validate)

    def visible(self, state=None):
        if self.memory.off or getattr(self.memory,'motivation_off',False):
            return {}
        state = self.ctl.state if state is None else state
        return {key: copy.deepcopy(value) for key, value in state['intentions'].items()
                if self.memory._intention_available(value) and self.memory._safe_item(value)}

    def proposal(self, pr, ctx):
        try:
            ops = self.checked(pr, ctx)
        except _DuplicateIntention:
            return KindDecision(violation=Violation('intention_duplicate', 'Такое открытое намерение уже сохранено'))
        except (KeyError, TypeError, ValueError):
            return KindDecision(violation=Violation('intention_invalid', 'Нужен доступный опыт, исходный критерий и подтверждённый результат'))
        self.grants.extend((op.group, op.path, op.value) for op in ops)
        return KindDecision(tuple(ops))

    def checked(self, pr, ctx):
        body = pr['body']
        op = body['op']
        iid = body['intention_id']
        if not identifier(iid) or 'intention' in ctx.accepted_kinds or 'no_action' in ctx.accepted_kinds:
            raise ValueError('Лимит намерений')
        visible = {item['ref'] for item in ctx.request['context']}
        rows = {r.memory_id: r for r in self.memory.records()} if not self.memory.off else {}
        if op == 'create':
            required={'op', 'intention_id', 'text', 'criterion', 'project_id', 'operation'}
            if not required <= set(body) or set(body)-required-{'not_before_life_ms'}:
                raise ValueError('Поля замысла')
            if iid in ctx.state['intentions'] or not text(body['text'], 500) or not text(body['criterion'], 500):
                raise ValueError('Нужна новая конкретная цель')
            if (not identifier(body['project_id']) or body['operation'] not in OPERATIONS
                    or OPERATIONS[body['operation']] not in ctx.request['allowed_actions']):
                raise ValueError('Нет доступного действия')
            if not pr['cites'] or any(ref not in visible or ref not in rows for ref in pr['cites']):
                raise ValueError('Основания скрыты')
            if not any(rows[ref].origin == 'observation' and rows[ref].content.get('experience') == 'live' for ref in pr['cites']):
                raise ValueError('Нужен собственный прожитый опыт')
            target = body['project_id']
            if body['operation'] == 'read' and target not in self.world.state.get('library', {}):
                raise ValueError('Нет материала')
            if body['operation'] == 'move' and target not in self.world.state.get('space', {}).get('objects', {}):
                raise ValueError('Нет предмета')
            if body['operation'] == 'write' and self.world.state.get('projects',{}).get(target,{}).get('status','active') != 'active':
                raise ValueError('Проект уже закрыт')
            when = body.get('not_before_life_ms')
            if 'not_before_life_ms' in body and (type(when) is not int or when < ctx.life_time_ms):
                raise ValueError('Нужен будущий момент собственного времени')
            signature = _signature(body)
            open_goals = (goal for goal in self.visible(ctx.state).values() if goal['status'] in OPEN_STATUSES)
            if any(_signature(goal) == signature for goal in open_goals):
                raise _DuplicateIntention()
            work = self.world.state.get('projects', {}).get(target, {}).get('latest_work')
            initial = self.world.state.get('works', {}).get(work, {}).get('fingerprint')
            value = {key: body[key] for key in ('text', 'criterion', 'project_id', 'operation')}
            value.update(status='active', sources=list(pr['cites']),
                         source_versions={ref: rows[ref].version for ref in pr['cites']},
                         created_seq=self.ctl.store.last_seq(), created_life_ms=ctx.life_time_ms,
                         initial_fingerprint=initial, knows=True, wants=True,
                         history=[{'status': 'active', 'reason': pr['rationale'], 'life_ms': ctx.life_time_ms}])
            if when is not None:
                value.update(not_before_life_ms=when,moment_id=new_id('mom'))
            return [Op('intentions', iid, 'set', value, True)]
        if op != 'transition' or set(body) - {'op', 'intention_id', 'to_status', 'result_ref'}:
            raise ValueError('Критерий/текст не переписываются')
        goal = self.visible(ctx.state)[iid]
        status = body['to_status']
        if status != 'done' and 'result_ref' in body:
            raise ValueError('Не нужен результат')
        ops = [Op('intentions', iid + '.status', 'transition', status, True),
               Op('intentions', iid + '.wants', 'set', status not in ('done', 'abandoned'), True),
               Op('intentions', iid + '.history', 'add', {'status': status, 'reason': pr['rationale'],
                                                        'life_ms': ctx.life_time_ms}, True)]
        if status == 'done':
            self.result(pr, ctx.request)
            from .completion import digest
            key = digest({'request': ctx.request, 'proposal': pr, 'goal': goal})
            assessed = self.reviews.get(key)
            if assessed is None or not assessed['passed']:
                raise ValueError('Завершение не подтверждено')
            ops += [Op('intentions', iid + '.completion', 'set', assessed, True)]
        return ops

    def result(self, pr, request):
        """Результат берётся из доступной версии памяти, а не из архива событий/работ."""
        goal = self.visible()[pr['body']['intention_id']]
        if goal['status']!='active':
            raise ValueError('Завершить можно только активное намерение')
        ref = pr['body']['result_ref']
        visible = {item['ref'] for item in request['context']}
        if ref not in visible or ref not in pr['cites']:
            raise ValueError('Результат не виден')
        rows = {r.memory_id: r for r in self.memory.records()}
        row = rows[ref]
        if row.origin != 'observation' or row.version != 1 or row.content.get('outcome') != 'confirmed':
            raise ValueError('Нет подтверждённого успеха')
        aid = row.content['action_id']
        action = self.world.state['actions'][aid]
        receipt = action['receipt']
        event = self.ctl.find_event(event_id=receipt['event_id'])
        if (action['status'] != 'confirmed' or receipt['target'] != goal['project_id']
                or action['capability_id'] != OPERATIONS[goal['operation']]
                or receipt['event_id'] not in row.sources or event.type != 'action.confirmed'
                or event.seq <= goal['created_seq'] or event.event_id not in pr['cites']
                or event.life_time_ms<goal.get('not_before_life_ms',0)):
            raise ValueError('Нужен поздний исход того же проекта и его событие в cites')
        if goal['operation'] == 'write' and receipt['fingerprint'] == goal['initial_fingerprint']:
            raise ValueError('Повтор исходного текста не является новой работой')
        if goal['operation'] == 'write' and receipt['fingerprint'] == receipt.get('previous_fingerprint'):
            raise ValueError('Повтор предыдущего текста не является новой работой')
        shown = []
        for item in request['context']:
            try:
                value = json.loads(item['text'])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                shown += value.get('world_results', [])
        displayed = next((item for item in shown if item.get('memory_id') == row.memory_id
                          and item.get('event_id') == event.event_id), None)
        if displayed is None or action['outcome'] != displayed['text']:
            raise ValueError('Результат не показан в текущем контексте')
        return goal, {'text': displayed['text'], 'scope': receipt['scope'], 'action_id': aid,
                      'memory_id': row.memory_id, 'memory_version': row.version, 'event_id': event.event_id}

    def validate(self, check):
        errors = []
        for change in check.changes:
            if change['group'] == 'intentions' and not any(
                    group == 'intentions' and path == change['path'] and value == change['after']
                    for group, path, value in self.grants):
                # add историю возвращает целиком: сопоставляем последнее добавленное событие.
                if not (change['path'].endswith('.history') and any(
                        group == 'intentions' and path == change['path'] and isinstance(change['after'], list)
                        and change['after'][-1:] == [value] for group, path, value in self.grants)):
                    errors.append('Намерение изменяется только проверенным типизированным предложением')
        return errors


__all__ = ['Intentions']
