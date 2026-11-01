"""Поступление → замечание → подтверждённое чтение; текст закрыт до чтения."""
import json

from iam.domain import Op, Transition, new_id
from iam.state import EventSpec, KindDecision, Proposal, Violation

STAGES = ('input.noticed', 'input.read', 'input.ignored')


class Perception:
    def __init__(self, controller, memory, *, level='N2', limit=32):
        if level not in ('N1', 'N2') or type(limit) is not int or limit < 1:
            raise ValueError('Уровень N1/N2 и положительный предел уведомлений обязательны')
        self.ctl, self.memory, self.level, self.limit = controller, memory, level, limit
        self._writing = None
        controller._perception_guard = self._guard
        controller.add_validator('perception_state', lambda check: controller._perception_guard.__self__._validate(check))

    def _guard(self, proposal, carrier):
        if (carrier.type in STAGES and proposal is not self._writing
                or any(spec.type in STAGES for spec in proposal.companions)):
            return [Violation('perception_reserved', 'Стадии восприятия записывает проверяющий модуль')]
        return []

    def _validate(self, check):
        def records(state):
            return state['world_time'].get('world', {}).get('perception', {})
        if records(check.before) != records(check.after) and self._writing is None:
            return ['Стадии восприятия нельзя менять обычным переходом']
        return []

    def _states(self):
        return self.ctl.state['world_time'].get('world', {}).get('perception', {})

    def _arrivals(self):
        # Архив транспорта читается здесь только для стадии восприятия, не как память.
        return {e.payload['input_id']: e for e in self.ctl.events_after(0)
                if e.type == 'input.arrived' and isinstance(e.payload.get('input_id'), str)}

    def notices(self, level=None):
        level = self.level if level is None else level
        if level not in ('N1', 'N2'):
            raise ValueError('Уровень уведомления N1 или N2')
        states = self._states()
        pending = self._pending()
        result = {'count': len(pending)}
        if level == 'N2':
            result['items'] = [{'input_id': iid, 'sender': e.payload.get('sender', ''),
                                'received_at': e.occurred_at, 'channel': e.payload.get('channel', '')}
                               for iid, e in pending[:self.limit] if states[iid]['level'] == 'N2']
        return result

    def _pending(self):
        states = self._states()
        pending = [(iid, e) for iid, e in self._arrivals().items()
                   if states.get(iid, {}).get('stage') in ('noticed', 'ignored')]
        return sorted(pending, key=lambda item: (states[item[0]]['stage'] == 'ignored', -item[1].seq))

    def _move(self, iid, stage, level, cause):
        value = {'stage': stage, 'level': level}
        tid = new_id('trn')
        key = {'noticed': f'ntc:{iid}:{level}', 'read': f'rd:{iid}', 'ignored': f'ign:{iid}'}[stage]
        t = Transition(tid, self.ctl.persona_id, self.ctl.epoch, self.ctl.state_version,
                       (Op('world_time', f'world.perception.{iid}', 'set', value, True),),
                       f'восприятие: {stage}', (cause,))
        p = Proposal(t, actor_ref='perception', carrier=EventSpec('input.' + stage, key,
                     {'input_id': iid, 'notice_level': level}))
        self._writing = p
        try:
            return self.ctl.apply(p)
        finally:
            self._writing = None

    def notice(self, input_id):
        arrival = self._arrivals().get(input_id)
        if arrival is None:
            raise ValueError('Неизвестное входящее')
        old = self._states().get(input_id, {})
        if old.get('stage') == 'read' or old.get('level') == 'N2' or old.get('level') == self.level:
            return None
        return self._move(input_id, 'noticed', self.level, arrival.event_id)

    def check_attend(self, pr, ctx, fallback):
        body = pr['body']
        if body['focus'] in ('event', 'dialogue'):
            iid = body.get('ref')
            visible = {x['ref'] for x in ctx.request['context'] if x['layer'] == 'notice'}
            if iid not in visible or self._states().get(iid, {}).get('stage') not in ('noticed', 'ignored'):
                return KindDecision(violation=Violation('input_not_visible', 'Можно открыть только замеченное входящее'))
        return fallback(pr, ctx)

    def _fresh(self, accepted, request):
        if (accepted.epoch != self.ctl.epoch or accepted.state_version != self.ctl.state_version
                or request['session_id'] != self.ctl.session_id):
            raise ValueError('Принятое решение устарело; требуется новый выбор')

    def finish(self, pr, accepted_id):
        if pr['kind'] not in ('attend', 'no_action'):
            return None
        accepted = self.ctl.find_event(event_id=accepted_id)
        if accepted is None or accepted.type != 'proposal.accepted' or accepted.payload['proposal_id'] != pr['proposal_id']:
            raise ValueError('Нет принятого решения')
        request = self.ctl.find_event(key='req:' + accepted.payload['request_id']).payload['request']
        response = self.ctl.find_event(key='resp:' + request['request_id']).payload['response']
        if pr not in response['proposals'] or self.ctl.session_state != 'running':
            raise ValueError('Решение не соответствует принятому ответу или сеанс остановлен')
        if pr['kind'] == 'attend' and pr['body']['focus'] in ('event', 'dialogue'):
            iid = pr['body']['ref']
            old = self._states()[iid]
            if old['stage'] == 'read':
                return None
            self._fresh(accepted, request)
            with self.ctl._atomic():
                result = self._move(iid, 'read', old['level'], accepted_id)
                if result.applied and not self.memory.off:
                    arrival = self._arrivals()[iid]
                    record = self.memory.prepare(kind='episodic', origin='stated_by_other',
                        text=arrival.payload.get('text', ''), sources=(result.events[0].event_id,),
                        claim_kind='fact_claim', independence_group='source:' + str(arrival.payload.get('sender', 'unknown')),
                        content={'experience': 'live'})
                    if not self.memory.write(record).applied:
                        raise RuntimeError('Прочитанное не удалось записать; чтение отменено')
                return result
        ids = [item['ref'] for item in request['context'] if item['layer'] == 'notice'
               and self._states().get(item['ref'], {}).get('stage') == 'noticed']
        if ids:
            self._fresh(accepted, request)
        results = []
        for iid in ids:
            results.append(self._move(iid, 'ignored', self._states()[iid]['level'], accepted_id))
        return results

    def context(self, preferred=None):
        notices = self.notices()
        states = self._states()
        # N1 использует непрозрачную ссылку, не имя, время, канал или превью.
        ids = [iid for iid, _ in self._pending()]
        if preferred in ids:
            ids.remove(preferred)
            ids.insert(0, preferred)
        ids = ids[:self.limit]
        items = {x['input_id']: x for x in notices.get('items', [])}
        return [{'ref': iid, 'origin': 'observation', 'layer': 'notice',
                 'text': json.dumps({'notice_level': states[iid]['level'], 'count': notices['count'],
                                     **items.get(iid, {})}, ensure_ascii=False)} for iid in ids]


__all__ = ['Perception']
