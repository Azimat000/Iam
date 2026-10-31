"""D-049: выбранный моделью критерий и проверяемое основание завершения.

Цель остаётся добровольной. Цитата подтверждает наличие результата, а не его
смысловое соответствие: последнее проверяется отдельно по сохранённому критерию.
"""
from dataclasses import replace
import json

from iam.domain import Op
from iam.experiments.development import MATERIALS, short
from iam.experiments.volition import INSTRUCTION as PREVIOUS, VolitionArchitecture, VolitionCycle
from iam.state import KindDecision, Violation

VERSION = 'commitment-v1'
OPERATIONS = ('read_material', 'open_project', 'write_project')
START = PREVIOUS.index('intention{op:"create"')
END = PREVIOUS.index('memory_write{draft:')
INSTRUCTION = PREVIOUS[:START] + (
    'intention{op:"create",id:ключ,text:свой_замысел,operation:read_material|open_project|write_project,'
    'target:ключ_материала_или_проекта,criterion:выбранный_тобой_признак_готовности}. '
    'Для read_material target берётся из materials, для open_project/write_project — ключ проекта, '
    'включая самостоятельно выбранный ключ будущего проекта. Сохранение цели добровольно. '
    'criterion хранится вместе с целью и не меняется при переходах статуса. '
    'intention{op:"transition",id:ключ,to_status:active|waiting_opportunity|deferred|abandoned}; '
    'допустимые переходы перечислены в intention_options. '
    'Для done новой цели: intention{op:"transition",id:ключ,to_status:"done",result_ref:evt_ссылка,'
    'result_quote:точная_непустая_цитата_результата}. result_ref — доступный evt_ ключ experience_sources, '
    'где completion_for содержит id цели; добавь его в cites. Нужен подтверждённый результат '
    'совпадающих operation/target после создания цели. result_quote должна быть дословно в этом '
    'результате и доступном тексте. Цитата не доказывает выполнения смысла criterion. '
    'Для старой цели без criterion сохраняется прежний формат done без result_quote; критерий ей не выдумывай. '
    'motivation.goal_opportunities показывает доступность принятой цели, в том числе до первого '
    'результата; goal_component — только прежний вклад цели, не дополнительная награда и не команда. '
) + PREVIOUS[END:]


class CommitmentCycle(VolitionCycle):
    def view(self):
        view = super().view()
        for key, item in view['intentions'].items():
            stored = self.ctl.state['intentions'][key]
            item.update(operation=stored.get('operation', 'write_project'), target=stored['project'],
                        criterion=stored.get('criterion'), criterion_recorded='criterion' in stored)
            item.pop('project')
        return view

    def _context(self, trigger, focus, recalled):
        context = super()._context(trigger, focus, recalled)
        context[0] = {**context[0], 'text': INSTRUCTION}
        for item in context:
            if item['origin'] == 'own_hypothesis' and item['layer'] == 'state' and item['text'].startswith('{"intention":'):
                key = json.loads(item['text'])['intention']
                item['text'] = json.dumps({'intention': key, **self.view()['intentions'][key]}, ensure_ascii=False)
        return context

    def signals(self, recalled):
        result = super().signals(recalled)
        if not result['enabled']:
            return result
        visible = {ref for item in result['items'] for mid in item['sources']
                   for ref in (mid, self.ctl.store.get_memory(mid).sources[0])}
        goals = {key: goal for key, goal in self.ctl.state['intentions'].items()
                 if goal['status'] == 'active' and set(goal['sources']) & visible}
        for item in result['items']:
            matching = [key for key, goal in goals.items() if item['activity'] ==
                        ('material:' if goal.get('operation') == 'read_material' else 'project:') + goal['project']]
            item['accepted_goals'] = matching
            item['salience'] = self.mechanism.score(item['inputs'], result['fixed_activity_need'], bool(matching))
        view = self.view()
        neutral = {'score':0.0, 'strength':0.0, 'novelty':0.0, 'skill':0.0}
        component = self.mechanism.score(neutral,0.0,True) - self.mechanism.score(neutral,0.0,False)
        result['goal_opportunities'] = []
        for key, goal in self.ctl.state['intentions'].items():
            if goal['status'] != 'active':
                continue
            operation, target = goal.get('operation','write_project'), goal['project']
            available = any(a['operation']==operation and a.get('target')==target for a in view['available_actions'])
            if operation=='write_project' and target not in view['projects']:
                available = view['project_creation']['can_create']
            activity = ('material:' if operation=='read_material' else 'project:') + target
            result['goal_opportunities'].append({'id':key,'operation':operation,'target':target,
                'basis_available':key in goals,'available':available,
                'activity_history_present':any(item['activity']==activity for item in result['items']),
                'goal_component':component if available and key in goals else 0.0})
        return result

    def _completion_ok(self, intention, source):
        if 'criterion' not in intention:
            return super()._completion_ok(intention, source)
        if intention['status'] != 'active' or source['operation'] != intention['operation']:
            return False
        event = self.ctl.find_event(event_id=source['base'])
        action = self.ctl.state['world_time']['world']['actions'][event.payload['action_id']]
        return event.seq > intention['created_seq'] and action['params']['target'] == intention['project']

    def _checked(self, pr, ctx):
        if pr['kind'] != 'intention':
            return super()._checked(pr, ctx)
        body = pr['body']; ident = body.get('id')
        old = ctx.state['intentions'].get(ident) if isinstance(ident, str) else None
        def bad():
            return KindDecision(violation=Violation('invalid_commitment', 'неверная цель, критерий или цитата результата'))
        quote = None
        if body.get('op') == 'create':
            if (set(body) != {'op','id','text','operation','target','criterion'}
                    or body['operation'] not in OPERATIONS or not short(body['criterion'], 500)
                    or not isinstance(body['target'], str)
                    or (body['operation'] == 'read_material' and body['target'] not in MATERIALS)):
                return bad()
            legacy = {k: body[k] for k in ('op','id','text')};legacy['project'] = body['target']
        elif body.get('to_status') == 'done' and old and 'criterion' in old:
            ref = body.get('result_ref'); source = self.evidence.get(ref) if isinstance(ref, str) else None
            quote = body.get('result_quote')
            if (set(body) != {'op','id','to_status','result_ref','result_quote'} or not short(quote, 500)
                    or not source or not self._completion_ok(old, source)):
                return bad()
            event = self.ctl.find_event(event_id=source['base'])
            outcome = self.ctl.state['world_time']['world']['actions'][event.payload['action_id']]['outcome']
            if quote not in source['text'] or quote not in outcome:
                return bad()
            legacy = {k:v for k,v in body.items() if k != 'result_quote'}
        else:
            return super()._checked(pr, ctx)
        decision = super()._checked({**pr, 'body': legacy}, ctx)
        if decision.violation:
            return decision
        if body['op'] == 'create':
            op = decision.ops[0]
            return KindDecision((replace(op, value={**op.value, 'operation':body['operation'], 'criterion':body['criterion']}),))
        return KindDecision((*decision.ops, Op('intentions',body['id']+'.result_quote','set',quote,True)))


class CommitmentArchitecture(VolitionArchitecture):
    protocol_version = VERSION

    def __init__(self, factory, mechanism='goals'):
        super().__init__(factory, mechanism)
        self.name = 'A-' + VERSION + '-' + mechanism

    def build(self, runtime):
        cycle = super().build(runtime)
        return CommitmentCycle(runtime, cycle.provider, self.mechanism)
