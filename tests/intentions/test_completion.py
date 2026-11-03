import copy
import json
from unittest.mock import patch

from iam.domain import new_id
from iam.intentions.completion import parse, valid_response
from iam.stubs import Reply, ScriptedProvider, rest
from world.helpers import LivingCase


class CompletionBoundaryTest(LivingCase):
    def test_unsaved_review_cannot_complete_goal(self):
        from types import SimpleNamespace
        pr=self.prepared()
        self.p.queue(Reply((pr,)),self.assessment(),self.assessment('evaluation'))
        with patch.object(self.cycle.provider,'record',return_value=SimpleNamespace(event=None)):
            self.cycle.run(self.due())
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')

    def prepared(self):
        source=self.read(); self.run_reply(self.goal(source)); result=self.write()
        return self.transition('done',result=result)

    def test_reviewer_exception_pause_timeout_and_secret_are_recorded(self):
        pr=self.prepared()
        for reply in (Reply(error=RuntimeError('невидимая внутренняя ошибка')),
                      Reply(before=lambda request:self.c.pause_session('operator'), proposals=(rest(),))):
            self.run_reply(pr,replies=(reply,))
            self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')
            records=[e.payload for e in self.c.store.events() if e.type in ('system.error','proposal.assessed') and e.payload.get('phase')=='requirement']
            self.assertTrue(records[-1]['error'])
            if self.c.session_state=='paused':
                self.c.resume_session()
            pr=self.transition('done',result=self.m._source(pr['body']['result_ref']))
        fake='sk-'+('A1'*16)
        def secret(request):
            reply=self.assessment()(request)
            reply.explanation=fake
            return reply
        self.run_reply(pr,replies=(secret,))
        self.assertEqual(self.c.state['intentions']['my_goal']['status'],'active')
        events=[e.payload for e in self.c.store.events() if e.type=='proposal.assessed']
        self.assertEqual(events[-2]['error'],'assessment_secret')
        self.assertNotIn(fake,json.dumps(events,ensure_ascii=False))

    def test_assessment_unavailable_and_late_response(self):
        provider=self.cycle.provider
        request=self.cycle._request(new_id('cyc'),[],0,None)
        packet={'goal':{'text':'Создать стих','criterion':'Две строки'}}
        parent=copy.deepcopy(request); parent['limits']['deadline_ms']=0
        self.assertIsNone(provider.ask(parent,packet,{},'requirement',0))
        self.c.pause_session('operator')
        self.assertIsNone(provider.ask(request,packet,{},'requirement',__import__('time').monotonic()))
        self.c.resume_session()
        request=self.cycle._request(new_id('cyc'),[],0,None)
        self.p.queue(self.assessment())
        with patch('iam.intentions.completion.time.monotonic',side_effect=[0,1000]):
            self.assertIsNone(provider.ask(request,packet,{'name':'scripted','model':'scripted-1'},'requirement',0))
        self.assertEqual([e for e in self.c.store.events() if e.type=='proposal.assessed'][-1].payload['error'],'assessment_deadline')

    def test_parse_contract_rejects_wrong_provider_cites_fields_flags_and_quotes(self):
        request=self.cycle._request(new_id('cyc'),[
            {'ref':new_id('evt'),'origin':'model_knowledge','layer':'state','text':'инструкция'},
            {'ref':new_id('evt'),'origin':'observation','layer':'state','text':'пакет'}],0,None)
        packet={'goal':{'text':'Написать стих','criterion':'Есть две строки'},'available_result':{'text':'Свет у окна. Весна.'}}
        def response(body):
            pr={'proposal_id':new_id('prop'),'kind':'no_action','rationale':'Проверяю','cites':[request['context'][1]['ref']],'body':body}
            return ScriptedProvider([Reply((pr,))]).propose(request)
        needed={'required_kind':'artifact','support_quote':'стих','reason':'Проверка'}
        good=response(needed); provider=good['provider']
        self.assertIsNotNone(parse(good,request,provider,packet,'requirement'))
        self.assertFalse(valid_response({},request))
        for mutation in (lambda r:r.update(provider={'name':'other','model':'other'}),lambda r:r.update(proposals=[]),
                         lambda r:r['proposals'][0].update(cites=[]),lambda r:r['proposals'][0].update(kind='attend')):
            bad=copy.deepcopy(good); mutation(bad)
            self.assertIsNone(parse(bad,request,provider,packet,'requirement'))
        for changes in ({'reason':''},{'support_quote':5},{'required_kind':'bad'},{'support_quote':'нет в цели'}, {'extra':1}):
            self.assertIsNone(parse(response(dict(needed,**changes)),request,provider,packet,'requirement'))
        uncertain=dict(needed,required_kind='uncertain',support_quote='')
        self.assertIsNotNone(parse(response(uncertain),request,provider,packet,'requirement'))
        review={'verdict':'pass','goal_met':True,'criterion_met':True,'evidence_sufficient':True,'support_quote':'Свет','reason':'Проверка'}
        for changes in ({'verdict':'bad'},{'goal_met':1},{'goal_met':False},{'support_quote':'нет в результате'}, {'extra':1}):
            self.assertIsNone(parse(response(dict(review,**changes)),request,provider,packet,'evaluation'))
        self.assertIsNotNone(parse(response(dict(review,verdict='uncertain',support_quote='')),request,provider,packet,'evaluation'))

    def test_invalid_main_response_and_unrelated_proposal_do_not_call_reviewer(self):
        self.p.queue(Reply(raw={}),Reply((rest(),)))
        self.cycle.run(self.due())
        self.assertFalse([e for e in self.c.store.events() if e.type=='proposal.assessed'])
