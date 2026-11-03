"""Постоянный текстовый мир. Вычисление результата отделено от его фиксации."""
import copy
import hashlib
import re

from iam.domain import Op
from iam.interfaces import ActionOutcome, Capability

READ = 'cap_Library01'
WRITE = 'cap_Project01'
MOVE = 'cap_RoomEdit01'
CAPABILITIES = (
    Capability(READ, 'Чтение библиотеки', 'params: material_id'),
    Capability(WRITE, 'Собственные работы', 'params: project_id, title, text'),
    Capability(MOVE, 'Личное пространство', 'params: object_id, place_id'),
)


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) is not None


def text(value, limit=8000):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def fingerprint(value):
    return hashlib.sha256(' '.join(value.split()).encode()).hexdigest()


class PersistentWorld:
    def __init__(self, controller, *, external=None):
        self.ctl, self.external = controller, external
        self.pending = {}
        controller.add_validator('persistent_world', self.validate)

    @property
    def state(self):
        return self.ctl.state['world_time'].get('world', {})

    def initialize(self):
        if 'space' in self.state:
            return
        result = self.ctl.intervene('technical_fix', 'Создано пустое личное пространство S22', (
            Op('world_time', 'world.space', 'set', {
                'places': {'desk': 'Рабочий стол', 'shelf': 'Полка'},
                'objects': {'notebook': {'title': 'Блокнот', 'place_id': 'desk'}}}, True),))
        if not result.applied:
            raise ValueError('Не удалось создать пространство')

    def add_material(self, title, content, reason):
        if self.ctl.session_state == 'running':
            raise ValueError('Поставьте сеанс на паузу перед загрузкой материала.')
        if not text(title, 160) or not text(content) or not text(reason, 500):
            raise ValueError('Нужны название до160, текст до8000 и причина до500 знаков.')
        from iam.domain import new_id
        mid = 'material_' + new_id('opr').split('_')[1]
        value = {'title': title, 'text': content, 'origin': 'operator_provided',
                 'fingerprint': fingerprint(content), 'read': False}
        result = self.ctl.intervene('technical_fix', reason, (
            Op('world_time', 'world.library.' + mid, 'set', value, True),))
        if not result.applied:
            raise ValueError('Материал не сохранён')
        return mid

    def check(self, request, state=None):
        state = self.state if state is None else state
        p = request.params
        if request.kind != 'action':
            raise ValueError('Нужен вид action')
        if request.capability_id == READ:
            if set(p) != {'material_id'} or not identifier(p['material_id']) or p['material_id'] not in state.get('library', {}):
                raise ValueError('Материал отсутствует')
        elif request.capability_id == WRITE:
            if (set(p) != {'project_id', 'title', 'text'} or not identifier(p['project_id'])
                    or not text(p['title'], 160) or not text(p['text'])):
                raise ValueError('Неверные параметры работы')
        elif request.capability_id == MOVE:
            space = state.get('space', {})
            if (set(p) != {'object_id', 'place_id'} or not identifier(p['object_id']) or not identifier(p['place_id'])
                    or p['object_id'] not in space.get('objects', {}) or p['place_id'] not in space.get('places', {})):
                raise ValueError('Предмет или место отсутствуют')
        else:
            raise ValueError('Неизвестное действие мира')

    def execute(self, request):
        if request.capability_id not in (READ, WRITE, MOVE):
            return self.external.execute(request) if self.external is not None else ActionOutcome('failed', 'Средство не подключено')
        try:
            self.check(request)
        except ValueError as exc:
            return ActionOutcome('failed', str(exc))
        started = self.ctl.life_time_ms
        p, state = request.params, self.state
        if request.capability_id == READ:
            material = state['library'][p['material_id']]
            summary, target, scope = material['text'], p['material_id'], 'received_text'
        elif request.capability_id == WRITE:
            summary, target, scope = p['text'], p['project_id'], 'artifact'
        else:
            summary, target, scope = 'Предмет помещён: ' + p['place_id'], p['object_id'], 'world_state'
        outcome = ActionOutcome('confirmed', summary)
        self.pending[request.action_id] = (request, outcome, target, scope, started)
        return outcome

    def result_ops(self, aid, outcome, event_id):
        """Только подготовленный этим миром исход может изменить объекты; обрыв ничего не пишет."""
        pending = self.pending.pop(aid, None)
        if pending is None or pending[1] != outcome or outcome.status != 'confirmed':
            return ()
        request, _, target, scope, started = pending
        p, state = request.params, self.state
        self.check(request, state)
        receipt = {'event_id': event_id, 'target': target, 'scope': scope,
                   'fingerprint': fingerprint(outcome.summary), 'duration_ms': max(0, self.ctl.life_time_ms - started)}
        ops = [Op('world_time', f'world.actions.{aid}.receipt', 'set', receipt, True)]
        if request.capability_id == READ:
            ops += [Op('world_time', f'world.library.{target}.read', 'set', True, True),
                    Op('world_time', f'world.library.{target}.read_event', 'set', event_id, True)]
        elif request.capability_id == WRITE:
            work = {'title': p['title'], 'text': p['text'], 'fingerprint': receipt['fingerprint'],
                    'event_id': event_id, 'action_id': aid}
            ops += [Op('world_time', 'world.works.' + aid, 'set', work, True),
                    Op('world_time', 'world.projects.' + target, 'set', {'title': p['title'], 'latest_work': aid}, True)]
        else:
            ops.append(Op('world_time', f'world.space.objects.{target}.place_id', 'set', p['place_id'], True))
        return tuple(ops)

    @staticmethod
    def validate(check):
        errors = []
        for change in check.changes:
            if change['group'] != 'world_time':
                continue
            path = change['path'].split('.')
            protected = path[0] == 'world' and (len(path) == 1 or path[1] in ('space', 'library', 'works', 'projects'))
            if protected and not (check.actor == 'operator' and check.carrier_type == 'operator.intervention'
                                  or check.actor == 'world' and check.carrier_type == 'action.confirmed'):
                errors.append('Объекты мира меняются только результатом мира или вмешательством оператора')
        return errors

    def view(self):
        return copy.deepcopy(self.state)


__all__ = ['PersistentWorld', 'READ', 'WRITE', 'MOVE', 'CAPABILITIES']
