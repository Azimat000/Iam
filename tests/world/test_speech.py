"""Speech is a bounded second decision, never a bypass around the core."""
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from iam.domain import new_id
from iam.interfaces import ProviderError
from iam.stubs import Reply, ScriptedProvider, say, rest, act
from iam.world.speech import compose, speech_context
from world.helpers import LivingCase


class SpeechTest(LivingCase):
    def setup_request(self):
        ref = self.read().memory_id
        request = self.cycle._request(new_id('cyc'), [
            {'ref': ref, 'origin': 'observation', 'layer': 'personal', 'text': 'Прочитанные слова целиком'}], 0, None)
        request['allowed_proposals'] = ['message', 'no_action', 'memory_write', 'reflection']
        message = say('cap_Telegram01', 'creator', 'Черновик', cites=[ref])
        response = ScriptedProvider([Reply((message,))]).propose(request)
        self.p.compose_message = self.p.propose
        return request, response

    def answer(self, request, text='А мне всё ещё нравится первое название.'):
        return Reply((say('cap_Telegram01', 'creator', text, cites=[request['context'][0]['ref']]),))

    def perform(self, request, response):
        return compose(self.cycle.provider, request, response, time.monotonic())

    def test_replaces_only_draft_preserves_other_choice_and_audits_sources(self):
        request, response = self.setup_request()
        other = {'proposal_id': new_id('prop'), 'kind': 'reflection', 'cites': [], 'body': {}, 'rationale': 'Обдумать'}
        response['proposals'].append(other)
        original = copy.deepcopy(request)
        self.p.queue(self.answer)
        result = self.perform(request, response)
        self.assertEqual(request, original)
        self.assertEqual(result['request_id'], request['request_id'])
        self.assertEqual(result['proposals'][0], other)
        self.assertNotEqual(result['proposals'][1]['body']['text'], 'Черновик')
        self.assertEqual(result['usage']['calls'], 2)
        child = self.p.calls[-1]
        self.assertNotEqual(child['request_id'], request['request_id'])
        self.assertEqual(child['purpose'], 'compose_message')
        self.assertNotIn('reflection', child['allowed_proposals'])
        self.assertEqual(child['allowed_actions'], ['cap_Telegram01'])
        self.assertLessEqual(child['limits']['deadline_ms'], request['limits']['deadline_ms'])
        audit = list(self.c.store.events())[-1].payload
        self.assertEqual(audit['phase'], 'conversation_composition')
        self.assertIsNone(audit['error'])
        self.assertEqual(audit['draft_proposal']['body']['text'], 'Черновик')

    def test_no_capability_no_message_other_purpose_or_invalid_draft_bypass(self):
        request, response = self.setup_request()
        del self.p.compose_message
        self.assertIs(self.perform(request, response), response)
        self.p.compose_message = self.p.propose
        for mutate in (lambda q,r:q.update(purpose='reflect'),
                       lambda q,r:q.update(allowed_proposals=['attend','no_action']),
                       lambda q,r:r.update(proposals=[rest()]),
                       lambda q,r:r['proposals'].append(act('cap_WorldRead01')),
                       lambda q,r:r['proposals'][0]['body'].update(extra='bad'),
                       lambda q,r:q.update(context=[])):
            q,r = copy.deepcopy(request),copy.deepcopy(response)
            mutate(q,r)
            self.assertIs(self.perform(q,r),r)

    def test_decline_note_slots_and_note_scope(self):
        request,response = self.setup_request()
        self.p.queue(Reply((rest('Передумала'),)))
        self.assertEqual(self.perform(request,response)['proposals'][0]['kind'],'no_action')
        def note(text='Не торопиться с советом'):
            return {'proposal_id':new_id('prop'),'kind':'memory_write','cites':[request['context'][0]['ref']],
                    'rationale':'Личный вывод','body':{'draft':{'kind':'procedural','origin':'own_hypothesis','content':{'text':text}}}}
        self.p.queue(Reply((note(),rest())))
        self.assertEqual([p['kind'] for p in self.perform(request,response)['proposals']],['memory_write'])
        for changes in ({'practice':{}}, {'skill_assessment':{}}, {'text':''}, {'text':'x'*513}):
            bad=note();bad['body']['draft']['content'].update(changes)
            self.p.queue(Reply((bad,rest())))
            with self.assertRaisesRegex(ProviderError,'composition_note'):
                self.perform(request,response)
        response['proposals'] += [note(),note()]
        self.p.queue(self.answer)
        self.perform(request,response)
        self.assertNotIn('memory_write',self.p.calls[-1]['allowed_proposals'])

    def test_rejects_invalid_envelope_recipient_sources_and_extra_actions(self):
        request,response=self.setup_request()
        for mutation,error in (
            (lambda a:a.update(epoch=999),'response'),
            (lambda a:a.update(provider={'name':'other','model':'other'}),'response'),
            (lambda a:a['proposals'][0]['body'].update(to='stranger'),'target'),
            (lambda a:a['proposals'][0]['body'].update(extra='x'),'target'),
            (lambda a:a['proposals'][0].update(cites=[new_id('mem')]),'scope'),
            (lambda a:a['proposals'].append(act('cap_WorldRead01')),'scope'),
            (lambda a:a['proposals'].append(rest()),'scope'),
            (lambda a:a.update(explanation='sk-'+('A1'*16)),'scope')):
            def reply(q):
                a=ScriptedProvider([self.answer(q)]).propose(q); mutation(a);return a
            self.p.queue(reply)
            with self.assertRaisesRegex(ProviderError,'composition_'+error):
                self.perform(request,response)
            self.assertTrue(list(self.c.store.events())[-1].payload['error'])
        self.assertNotIn('sk-'+('A1'*16),json.dumps([e.payload for e in self.c.store.events()]))

    def test_pause_expiry_and_failed_audit_never_return_original_draft(self):
        request,response=self.setup_request()
        with patch('iam.world.speech.time.monotonic',return_value=1000):
            with self.assertRaisesRegex(ProviderError,'unavailable'):
                compose(self.cycle.provider,request,response,0)
        self.p.queue(self.answer)
        with patch('iam.world.speech.time.monotonic',side_effect=[0,1000]):
            with self.assertRaisesRegex(ProviderError,'state_or_deadline'):
                compose(self.cycle.provider,request,response,0)
        self.p.queue(self.answer)
        record=self.cycle.provider.record
        records=[]
        def failing_record(rid,value):
            records.append(value)
            return record(rid,value) if len(records)==1 else SimpleNamespace(event=None)
        with patch.object(self.cycle.provider,'record',side_effect=failing_record):
            with self.assertRaisesRegex(ProviderError,'audit_unavailable'):
                self.perform(request,response)
        self.p.queue(Reply((rest(),),before=lambda q:self.c.pause_session('operator')))
        with self.assertRaisesRegex(ProviderError,'state_or_deadline'):
            self.perform(request,response)
        self.assertEqual(list(self.c.store.events())[-1].type,'system.error')
        with self.assertRaisesRegex(ProviderError,'unavailable'):
            self.perform(request,response)

    def test_projection_keeps_full_words_author_order_character_and_own_notes(self):
        def item(value,layer='state'):
            return {'ref':new_id('mem'),'origin':'observation','layer':layer,
                    'text':json.dumps(value,ensure_ascii=False) if not isinstance(value,str) else value}
        full=item({'text':'Длинная реплика '+('слова '*100),'sources':['evt_Basis001'],'window':{'more':False}},'personal')
        dialogue=item({'kind':'dialogue_turn','direction':'incoming','received_seq':14,'seq':25})
        current=item({'kind':'current_interaction','guidance':'huge rules','text':'Текущий вопрос'})
        character=item({'kind':'character','traits':[{'name':'openness','value':0.5}]})
        catalog=item({'world_results':[{'text':'Моя работа целиком'}],'intentions':{'old':'huge'},'capabilities':['many']})
        context=[full,dialogue,current,character,catalog,item('Служебный текст'),item([]),item('Личный вывод','interpretation'),item({'x':1},'notice')]
        before=copy.deepcopy(context); result=speech_context(context)
        self.assertEqual(context,before)
        self.assertIn(full,result);self.assertIn(dialogue,result);self.assertIn(character,result)
        self.assertEqual(len(result),7)
        encoded=json.dumps(result,ensure_ascii=False)
        self.assertNotIn('huge',encoded);self.assertNotIn('Служебный',encoded)
        self.assertIn('Моя работа целиком',encoded);self.assertIn('Личный вывод',encoded)
        for status in ('ambiguous','unresolved','unavailable'):
            restriction=item({'kind':'discussion_project','status':status,'meaning':'Другая работа не отвечает на вопрос'})
            self.assertIn(restriction,speech_context([catalog,restriction]))

    def test_composition_request_survives_interruption_and_requires_preflight_audit(self):
        request,response=self.setup_request()
        before=len(self.p.calls)
        with patch.object(self.cycle.provider,'record',return_value=SimpleNamespace(event=None)):
            with self.assertRaisesRegex(ProviderError,'audit_unavailable'):
                self.perform(request,response)
        self.assertEqual(len(self.p.calls),before)
        self.p.queue(Reply(error=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            self.perform(request,response)
        event=list(self.c.store.events())[-1]
        self.assertEqual(event.payload['phase'],'conversation_composition_requested')
        self.assertEqual(event.payload['parent_request_id'],request['request_id'])
        self.assertEqual(event.payload['request'],self.p.calls[-1])

    def test_panel_provider_chain_keeps_composition_metered_and_pausable(self):
        from iam.session import SessionRunner
        from iam.providers import PausingProvider
        request,response=self.setup_request()
        session=SessionRunner(self.c)
        before=session.usage()['calls']
        wrapped=session.provider(self.p)
        self.cycle.provider.inner=wrapped
        self.p.queue(self.answer)
        result=self.perform(request,response)
        self.assertNotEqual(result['proposals'][0]['body']['text'],'Черновик')
        self.assertEqual(session.usage()['calls'],before+1)
        self.assertEqual(session.request_id,self.p.calls[-1]['request_id'])
        self.c.pause_session('operator')
        with self.assertRaises(ProviderError):
            wrapped.compose_message(request)
        del self.p.compose_message
        self.assertIsNone(session.provider(self.p).compose_message)
        self.assertIsNone(PausingProvider(self.p,self.c).compose_message)

    def test_codex_composition_uses_same_model_runner_and_rejects_wrong_purpose(self):
        from providers.test_codex import provider, fake, NOTE, request
        inner,runner=provider(fake.reply([NOTE]))
        with self.assertRaises(ValueError):
            inner.compose_message(request())
        result=inner.compose_message(request(purpose='compose_message'))
        self.assertEqual(result['provider']['model'],'gpt-6-luna')
        self.assertEqual(result['usage']['calls'],1)
        self.assertEqual(len(runner.calls),1)
