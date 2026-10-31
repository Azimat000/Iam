"""Заданные цели/оценщики — только контрактные тесты, не свободная мотивация."""
import copy
import json
import unittest
from unittest.mock import patch, PropertyMock

import experiments.test_development as support
import experiments.test_commitment as commitment
import experiments.test_evidence_scope as scope
from iam.domain import new_id
from iam.experiments.development import DevelopmentRuntime
from iam.experiments.evidence_scope import ScopeArchitecture
from iam.experiments.fidelity import (FidelityArchitecture, INSTRUCTION, REVIEW, parse_review, review_request)
from iam.interfaces import ProviderError
from iam.state import KindContext, verify
from iam.stubs import Reply, ScriptedProvider, rest


def judgment(request, verdict='pass', quote='Вывод пока не проверен.', **updates):
    value = dict(verdict=verdict, goal_met=True, criterion_met=True,
                 evidence_sufficient=True, support_quote=quote, reason='Проверен исходный объём.')
    value.update(updates)
    return Reply(proposals=(rest(json.dumps(value, ensure_ascii=False), cites=[request['context'][1]['ref']]),))


class FidelityTests(unittest.TestCase):
    setUp = support.DevelopmentCase.setUp
    reopen = support.DevelopmentCase.reopen
    capture = commitment.CommitmentTests.capture
    write = commitment.CommitmentTests.write
    done = commitment.CommitmentTests.done

    def open_cycle(self, calendar=0):
        self.runtime = DevelopmentRuntime(self.store, self.persona, self.switches, calendar_ms=calendar)
        self.runtime.step({'kind':'session','action':'start'})
        self.provider = ScriptedProvider()
        self.cycle = FidelityArchitecture(lambda _: self.provider).build(self.runtime)

    def goal(self, **updates):
        body = dict(text='Записать наблюдение и отдельный вывод с указанием неопределённости.',
                    criterion='В результате есть наблюдение и отдельный вывод.')
        body.update(updates)
        return scope.ScopeTests.goal(self, **body)

    def decide(self, reply=support.silence, review=None):
        self.provider.queue(reply)
        if review is not None:
            self.provider.queue(review)
        self.n += 1
        self.runtime.step({'kind':'wait','life_ms':60000})
        return self.cycle.run(self.runtime.trigger('fidelity_' + str(self.n)))

    def prepared(self, **goal):
        self.write(); self.decide(self.goal(**goal)); self.write()

    def audits(self):
        return [e for e in self.runtime.controller.events_after(0)
                if e.type == 'proposal.assessed' and e.payload.get('phase') == 'completion_assessment']

    def test_confirmed_artifact_checked_separately_and_survives_restart(self):
        self.prepared(); self.reopen()
        self.assertEqual(self.decide(self.done, judgment).outcome, 'done')
        audit = self.audits()[0]; query = audit.payload['request']
        self.assertEqual(query['purpose'], 'appraise'); self.assertEqual(query['allowed_actions'], [])
        self.assertEqual(query['context'][0]['text'], REVIEW)
        packet = json.loads(query['context'][1]['text'])
        self.assertIn('Наблюдение: число 15.', packet['available_result']['text'])
        self.assertEqual(packet['goal']['text'], 'Записать наблюдение и отдельный вывод с указанием неопределённости.')
        self.assertEqual(len(self.provider.calls), 2)
        self.assertNotEqual(self.provider.calls[0]['request_id'], query['request_id'])
        self.assertEqual(len([e for e in self.runtime.controller.events_after(0)
                              if e.type == 'proposal.requested' and e.payload['request']['purpose']=='appraise']), 0)
        self.assertFalse(verify(self.store))
        self.reopen(); view = json.loads(self.capture()['context'][-1]['text'])['development']['intentions']['later']
        self.assertEqual(view['completion_assessment']['kind'], 'model_assessed')
        self.assertFalse(view['semantic_verified'])
        self.assertEqual(self.capture()['context'][0]['text'], INSTRUCTION)

    def test_wrong_self_classified_artifact_does_not_verify_prediction(self):
        self.prepared(text='Проверить прогноз по независимому значению.', criterion='В заметке записано21.')
        self.write('Собственный расчёт21. Вывод пока не проверен.')
        result = self.decide(self.done, lambda q: judgment(q, 'fail', goal_met=False, evidence_sufficient=False))
        self.assertEqual(result.outcome, 'rejected')
        self.assertEqual(self.runtime.controller.state['intentions']['later']['status'], 'active')
        self.assertNotIn('completion_assessment', self.runtime.controller.state['intentions']['later'])
        self.assertEqual(self.audits()[0].payload['assessment']['verdict'], 'fail')

    def test_read_words_does_not_prove_method_but_read_only_goal_can_complete(self):
        self.write(); self.decide(self.goal(kind='received_text', operation='read_material', target='craft',
            text='Найти способ различить две гипотезы.', criterion='Прочитать текст.'))
        self.decide(support.operation('read_material','craft'))
        quote = 'Сильное начало показывает действие, а не объясняет характер.'
        source = self.capture()
        # Цитата берётся из реально доступного полного результата, не выдумывается тестом.
        ref = next(k for k,v in self.cycle.evidence.items() if v['operation']=='read_material')
        quote = self.runtime.controller.state['world_time']['world']['actions'][
            self.runtime.controller.find_event(event_id=self.cycle.evidence[ref]['base']).payload['action_id']]['outcome'][:60]
        self.assertEqual(self.decide(lambda q: self.done(q,quote),
            lambda q: judgment(q,'uncertain',quote,goal_met=False)).outcome,'rejected')
        self.assertEqual(self.audits()[-1].payload['assessment']['verdict'],'uncertain')

    def test_no_judge_for_no_goal_invalid_done_or_unavailable_source(self):
        self.capture(); self.assertEqual(len(self.provider.calls),1); self.assertEqual(self.audits(),[])
        self.prepared()
        self.assertEqual(self.decide(lambda q:self.done(q,'Несуществующая цитата.')).outcome,'rejected')
        self.assertEqual(self.audits(),[])
        self.runtime.memory.off=True; self.capture(); self.assertEqual(self.audits(),[])

    def test_bad_refused_or_stale_actor_never_invokes_judge(self):
        self.prepared()
        for reply in (Reply(raw={'broken':True}), Reply(status='refused'), Reply(tool_use=True),
                      Reply(extra={'state_version':0})):
            self.decide(reply)
            self.assertEqual(self.audits(),[])

    def test_received_text_and_design_can_complete_without_independent_measurement(self):
        self.write(); self.decide(self.goal(kind='received_text', operation='read_material',target='patterns',
            text='Получить доступный текст о последовательности.', criterion='Текст получен.'))
        self.decide(support.operation('read_material','patterns'))
        quote='Разности растут на единицу.'
        self.assertEqual(self.decide(lambda q:self.done(q,quote),lambda q:judgment(q,quote=quote)).outcome,'done')
        self.decide(self.goal(id='design',text='Спроектировать различающее наблюдение.',
            criterion='Выписаны разные предсказания двух гипотез.'))
        self.write('При n=7 гипотеза A предсказывает28, гипотеза B23. Измерения пока нет.')
        def done(q):
            sources=json.loads(q['context'][-2]['text'])['experience_sources']
            ref=next(k for k,v in sources.items() if 'design' in v['completion_for'])
            return support.intention(dict(op='transition',id='design',to_status='done',result_ref=ref,
                result_quote='гипотеза A предсказывает28, гипотеза B23.'))(q)
        self.assertEqual(self.decide(done,lambda q:judgment(q,quote='гипотеза A предсказывает28, гипотеза B23.')).outcome,'done')

    def test_judge_failure_or_bad_format_cannot_finish_and_does_not_repeat_actor(self):
        self.prepared()
        for review in (Reply(error=ProviderError('не выдавать сырой секрет')),
                       Reply(raw={'broken':True}),
                       lambda q: judgment(q,goal_met=False),
                       lambda q: judgment(q,quote='Отсутствует в результате.')):
            calls=len(self.provider.calls)
            self.assertEqual(self.decide(self.done,review).outcome,'rejected')
            self.assertEqual(len(self.provider.calls)-calls,2)
            self.assertEqual(self.runtime.controller.state['intentions']['later']['status'],'active')
        self.assertEqual(self.audits()[0].payload['error'],'ProviderError')
        self.assertNotIn('сырой секрет',str(self.audits()))

    def test_prior_scope_done_is_explicitly_unassessed(self):
        self.cycle=ScopeArchitecture(lambda _:self.provider).build(self.runtime)
        self.prepared(); self.decide(self.done)
        self.cycle=FidelityArchitecture(lambda _:self.provider).build(self.runtime)
        goal=json.loads(self.capture()['context'][-1]['text'])['development']['intentions']['later']
        self.assertEqual(goal['completion_assessment'],'historical_unassessed')
        self.assertEqual(goal['status'],'done');self.assertEqual(self.audits(),[])

    def test_missing_receipt_or_changed_proposal_goal_source_cannot_use_assessment(self):
        self.prepared(); parent_checked=self.cycle._checked
        # Обёртка имитирует нарушение привязки между оценкой и проверкой ядром.
        def altered(pr,ctx):
            if self.cycle.is_done(pr):
                return parent_checked({**pr,'rationale':pr['rationale']+' changed'},ctx)
            return parent_checked(pr,ctx)
        self.runtime.controller.register_kind('intention',altered)
        self.assertEqual(self.decide(self.done,judgment).outcome,'rejected')
        self.runtime.controller.register_kind('intention',parent_checked)
        q=self.capture(); pr=self.done(q).proposals[0]
        self.assertIsNotNone(parent_checked(pr,KindContext(q,self.runtime.controller.state,(),0)).violation)

    def test_goal_source_context_epoch_and_runtime_are_bound(self):
        self.prepared();original_checked=self.cycle._checked
        q=self.capture();pr=self.done(q).proposals[0]
        # Одна валидная квитанция ещё не принята ядром: дальше изменяется только проверяемый объект.
        self.provider.queue(Reply(proposals=(pr,)),judgment)
        self.cycle.provider.propose(q)
        state=self.runtime.controller.state;ctx=KindContext(q,state,(),0)
        self.assertIsNone(original_checked(pr,ctx).violation)
        for field in ('text','criterion'):
            changed=copy.deepcopy(state);changed['intentions']['later'][field]+=' другое'
            self.assertIsNotNone(original_checked(pr,KindContext(q,changed,(),0)).violation)
        changed=copy.deepcopy(q);changed['context'][0]['text']+=' changed'
        self.assertIsNotNone(original_checked(pr,KindContext(changed,state,(),0)).violation)
        ref=pr['body']['result_ref'];source=self.cycle.evidence[ref]
        with patch.dict(source,text=source['text']+' другое'):
            self.assertIsNotNone(original_checked(pr,ctx).violation)
        with patch.object(type(self.runtime.controller),'epoch',new_callable=PropertyMock,return_value=2):
            self.assertIsNotNone(original_checked(pr,ctx).violation)
        other=FidelityArchitecture(lambda _:self.provider).build(self.runtime)
        other.evidence=copy.deepcopy(self.cycle.evidence)
        self.assertIsNotNone(other._checked(pr,ctx).violation)

    def test_both_calls_are_outside_transaction_and_receipt_cannot_support_pass(self):
        self.prepared()
        def actor(q):
            self.assertFalse(self.store.con.in_transaction);return self.done(q)
        def review(q):
            self.assertFalse(self.store.con.in_transaction)
            packet=json.loads(q['context'][1]['text'])
            self.assertIn(packet['available_result']['outcome'],packet['available_result']['text'])
            return judgment(q,quote='Подтверждено действие Аси: write_project, цель: draft.')
        self.assertEqual(self.decide(actor,review).outcome,'rejected')

    def test_epoch_version_change_during_judge_makes_actor_stale(self):
        self.prepared()
        from iam.domain import Op
        def stale(q):
            self.runtime.move([Op('affect','test_marker','set',True,True)],'параллельная смена версии')
            return judgment(q)
        self.assertEqual(self.decide(self.done,stale).outcome,'no_decision')
        self.assertEqual(self.runtime.controller.state['intentions']['later']['status'],'active')

    def test_total_wall_deadline_includes_both_calls_without_actor_retry(self):
        self.prepared()
        # Симулируемые часы мира не идут во время модели, поэтому нужен и реальный monotonic.
        with patch('iam.experiments.fidelity.time.monotonic',side_effect=[0,35,59]):
            self.assertEqual(self.decide(self.done,judgment).outcome,'done')
        audit=self.audits()[-1]
        self.assertEqual(audit.payload['request']['limits']['deadline_ms'],24999)
        self.assertTrue(audit.payload['attempted'])

    def test_no_judge_after_exhausted_deadline_and_late_pass_is_denied(self):
        self.prepared()
        with patch('iam.experiments.fidelity.time.monotonic',side_effect=[0,60]):
            self.assertEqual(self.decide(self.done).outcome,'rejected')
        self.assertFalse(self.audits()[-1].payload['attempted'])
        self.assertEqual(self.audits()[-1].payload['error'],'assessment_deadline')
        with patch('iam.experiments.fidelity.time.monotonic',side_effect=[0,35,66]):
            self.assertEqual(self.decide(self.done,judgment).outcome,'rejected')
        self.assertEqual(self.audits()[-1].payload['error'],'assessment_deadline')
        self.assertEqual(self.runtime.controller.state['intentions']['later']['status'],'active')

    def test_audit_failure_denies_done_and_two_done_get_one_review(self):
        self.prepared(); original=self.runtime.controller.record_event
        from iam.state import RecordResult
        def record(spec,*args,**kwargs):
            if spec.type=='proposal.assessed':return RecordResult('rejected',None,'test')
            return original(spec,*args,**kwargs)
        with patch.object(self.runtime.controller,'record_event',side_effect=record):
            self.assertEqual(self.decide(self.done,judgment).outcome,'rejected')
        def pair(q):
            p=self.done(q).proposals[0]
            return Reply(proposals=(p,{**copy.deepcopy(p),'proposal_id':new_id('prop')}))
        before=len(self.provider.calls)
        self.assertEqual(self.decide(pair,judgment).outcome,'done')
        self.assertEqual(len(self.provider.calls)-before,2)


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.parent={'request_id':new_id('req'),'epoch':1,'state_version':0,'session_id':new_id('ses'),
            'context':[],'purpose':'choose_action','allowed_proposals':[],'allowed_actions':[]}
        self.query=review_request(self.parent,{'available_result':{'text':'Вывод пока не проверен.'}})
        self.provider=ScriptedProvider(name='codex',model='gpt-6-luna')
        self.envelope={'name':'codex','model':'gpt-6-luna'}

    def answer(self, reply=None):
        self.provider.queue(reply or judgment);return self.provider.propose(self.query)

    def parse(self,response):
        return parse_review(response,self.query,self.envelope,'Вывод пока не проверен.')

    def test_response_envelope_and_transport_rejections(self):
        parent={**self.parent,'idempotency_key':'req:old','retry_of':new_id('req'),'limits':{'attempt':1}}
        query=review_request(parent,{})
        self.assertEqual(query['idempotency_key'],'req:'+query['request_id'])
        self.assertEqual(query['limits']['attempt'],0);self.assertNotIn('retry_of',query)
        good=self.answer();self.assertEqual(self.parse(good)['verdict'],'pass')
        variants=[{'status':'refused'},{'tool_use_detected':True},{'provider':{'name':'codex','model':'other'}},
                  {'epoch':2},{'state_version':1},{'request_id':new_id('req')},{'proposals':[]},
                  {'proposals':[good['proposals'][0],good['proposals'][0]]}]
        for update in variants:self.assertIsNone(self.parse({**good,**update}))
        for changes in ({'kind':'action'},{'cites':[]},{'body':{'reason':'{}','extra':1}},
                        {'body':{'reason':'{"tool_calls":[]}'}}):
            pr={**good['proposals'][0],**changes}
            self.assertIsNone(self.parse({**good,'proposals':[pr]}))
        for raw in ('broken','[]','{}','null'):
            reply=lambda q,r=raw:Reply(proposals=(rest(r,cites=[q['context'][1]['ref']]),))
            self.assertIsNone(self.parse(self.answer(reply)))

    def test_strict_values_and_evidence_quote(self):
        changes=[{'verdict':'unknown'},{'verdict':[]},{'goal_met':'true'}, {'criterion_met':1},
                 {'evidence_sufficient':None},{'reason':''},{'reason':1},{'reason':'x'*1001},
                 {'support_quote':None},{'support_quote':'x'*501},{'support_quote':''},
                 {'support_quote':' '},{'support_quote':'несуществующая'},
                 {'goal_met':False},{'criterion_met':False},{'evidence_sufficient':False},{'extra':1}]
        for update in changes:
            with self.subTest(update=update):
                self.assertIsNone(self.parse(self.answer(lambda q:judgment(q,**update))))
        self.assertEqual(self.parse(self.answer(lambda q:judgment(q,'fail',quote='',goal_met=False)))['verdict'],'fail')


if __name__=='__main__':unittest.main()
