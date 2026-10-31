"""D050: артефакт/полученный текст не являются независимым наблюдением."""
from dataclasses import replace
import json

from iam.domain import Op
from iam.experiments.commitment import INSTRUCTION as PREVIOUS, CommitmentArchitecture, CommitmentCycle
from iam.state import KindDecision, Violation

VERSION = 'scope-v1'
KINDS = ('artifact','received_text','independent_observation')
INSTRUCTION = PREVIOUS.replace('criterion:выбранный_тобой_признак_готовности}',
    'criterion:выбранный_тобой_признак_готовности,evidence_kind:artifact|received_text|independent_observation}') + (
    ' evidence_kind фиксирует требуемый вид свидетельства вместе с критерием. artifact — собственный '
    'текст или его доступная версия; received_text — получение предоставленного текста; '
    'independent_observation — независимо полученное новое наблюдение. Текущие операции чтения, '
    'открытия и записи новых независимых наблюдений не получают. Собственный расчёт или повтор '
    'предсказания в заметке не проверяет это предсказание. Прочитанная цитата сама по себе не '
    'доказывает понимания. Цель с пока недоступным свидетельством допустима; её доступность0, '
    'а done по другому виду результата не применяется. Изменение критерия/вида требует явно '
    'оставить прежнюю цель и создать новую, если ты этого хочешь. semantic_verified=false '
    'означает отсутствие независимой смысловой проверки, даже когда твой done технически принят. '
)


class ScopeCycle(CommitmentCycle):
    def source_scopes(self, source):
        operation = source['operation']
        if operation == 'write_project':
            return ['artifact']
        if operation == 'read_material':
            return ['received_text']
        if operation == 'open_project':
            own = self.provenance.get(source['base'],{}).get('content_origin') in ('model_written_version','scripted_version')
            return ['received_text','artifact'] if own else ['received_text']
        return []

    def can_supply(self, goal):
        kind, operation = goal.get('evidence_kind'), goal.get('operation','write_project')
        if kind is None:
            return True
        if kind == 'received_text':
            return operation in ('read_material','open_project')
        if kind == 'artifact':
            if operation == 'write_project':
                return True
            project = self.world_state['projects'].get(goal['project'])
            return bool(operation == 'open_project' and project and self.provenance.get(
                project['versions'][-1]['source'],{}).get('content_origin') in ('model_written_version','scripted_version'))
        return False

    def view(self):
        view = super().view()
        view['result_scopes'] = {'write_project':['artifact'],'read_material':['received_text'],
            'open_project':['received_text','artifact_if_own_version'],'independent_observation_available':False}
        for key, item in view['intentions'].items():
            stored=self.ctl.state['intentions'][key]
            item.update(evidence_kind=stored.get('evidence_kind'),semantic_verified=False)
        return view

    def _context(self, trigger, focus, recalled):
        context=super()._context(trigger,focus,recalled);context[0]={**context[0],'text':INSTRUCTION}
        value=json.loads(context[-2]['text'])
        for ref,item in value['experience_sources'].items():
            item['result_scopes']=self.source_scopes(self.evidence[ref])
        context[-2]={**context[-2],'text':json.dumps(value,ensure_ascii=False)}
        return context

    def signals(self, recalled):
        result=super().signals(recalled)
        if not result['enabled']:
            return result
        goals=self.ctl.state['intentions']
        for item in result['items']:
            item['accepted_goals']=[key for key in item['accepted_goals'] if self.can_supply(goals[key])]
            item['salience']=self.mechanism.score(item['inputs'],result['fixed_activity_need'],bool(item['accepted_goals']))
        for item in result['goal_opportunities']:
            item['operation_available']=item['available'];item['evidence_available']=self.can_supply(goals[item['id']])
            item['available']=item['available'] and item['evidence_available']
            if not item['available']:item['goal_component']=0.0
            item['component_in_historical_salience']=any(item['id'] in x['accepted_goals'] for x in result['items'])
        return result

    def _completion_ok(self, intention, source):
        return super()._completion_ok(intention,source) and (
            'evidence_kind' not in intention or intention['evidence_kind'] in self.source_scopes(source))

    def _checked(self, pr, ctx):
        if pr['kind']!='intention':return super()._checked(pr,ctx)
        body=pr['body']
        if body.get('op')=='create':
            if body.get('evidence_kind') not in KINDS:
                return KindDecision(violation=Violation('invalid_evidence_scope','не указан вид требуемого свидетельства'))
            decision=super()._checked({**pr,'body':{k:v for k,v in body.items() if k!='evidence_kind'}},ctx)
            if decision.violation:return decision
            op=decision.ops[0]
            return KindDecision((replace(op,value={**op.value,'evidence_kind':body['evidence_kind']}),))
        decision=super()._checked(pr,ctx)
        if not decision.violation and body.get('to_status')=='done':
            return KindDecision((*decision.ops,Op('intentions',body['id']+'.semantic_verified','set',False,True)))
        return decision


class ScopeArchitecture(CommitmentArchitecture):
    protocol_version=VERSION

    def __init__(self,factory,mechanism='goals'):
        super().__init__(factory,mechanism);self.name='A-'+VERSION+'-'+mechanism

    def build(self,runtime):
        cycle=super().build(runtime)
        return ScopeCycle(runtime,cycle.provider,self.mechanism)
