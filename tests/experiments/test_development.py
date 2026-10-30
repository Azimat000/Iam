"""Развитие связано с доступным опытом; декларации и технический успех не заменяют его."""
import copy
from dataclasses import replace
import json
import runpy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from iam.domain import new_id, Op
from iam.experiments import Persona
from iam.experiments.conversation import CAP_DIALOGUE, records
from iam.experiments.development import (AXES, CAP_DEVELOPMENT, KINDS, MATERIALS, DevelopmentArchitecture,
                                         DevelopmentRuleProvider, DevelopmentRuntime)
from iam.experiments.report.development import (DAY, PROBE, SessionBudget, assess_development, biography,
    development_packet, feedback, fork_checkpoint, run_development, save_trace)
from iam.experiments.report.__main__ import main
from iam.experiments.results import RunResult
from iam.experiments.variants.model_led.provider import GroundedProvider
from iam.interfaces import ActionOutcome, ActionRequest, ProviderError
from iam.providers.errors import ProviderFormatError, SessionLimitReached
from iam.state import KindContext, verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, rest, say

ROOT = Path(__file__).resolve().parents[2]
SWITCHES = {'memory_off': False, 'motivation_off': False, 'interests_off': False, 'relationships_off': False}


def refs(req):
    return list(dict.fromkeys(c['ref'] for c in req['context']))


def operation(name, target='', **params):
    return lambda req: Reply(proposals=(act(CAP_DEVELOPMENT, {'operation': name, 'target': target, **params}, cites=refs(req)),))


def silence(req):
    return Reply(proposals=(rest('Я пока подумаю.', cites=refs(req)),))


def speech(req):
    return Reply(proposals=(say(CAP_DIALOGUE, 'operator', 'Я хочу продолжить свою работу.', cites=refs(req)),))


def appraisal(req, domain='interest', key=None, values=None, text='Мне интересно пересматривать собственный текст.', source=None, quote=None):
    key = key or {'interest': 'creative_work', 'relationship': 'operator', 'self': 'identity'}[domain]
    if source is None:
        sources = json.loads(req['context'][-2]['text'])['experience_sources']
        source = next(c for c in req['context'] if c['layer'] == 'personal' and c['origin'] in ('observation', 'stated_by_other')
                      and (domain != 'self' or sources.get(c['ref'], {}).get('self_quote')))
        if domain == 'self' and quote is None:
            quote = sources[source['ref']]['self_quote']
    values = values if values is not None else ({'strength': 0.2} if domain == 'interest' else
             {axis: default for axis, (_, _, default) in AXES.items()} if domain == 'relationship' else {})
    return {'proposal_id': new_id('prop'), 'kind': 'memory_write', 'rationale': 'Осмысление конкретного опыта',
            'cites': refs(req), 'body': {'draft': {'kind': KINDS[domain], 'origin': 'own_hypothesis', 'content': {
                'domain': domain, 'key': key, 'label': 'Моё осмысление', 'values': values, 'text': text,
                'quote': quote if quote is not None else source['text'][:60], 'source_ref': source['ref']}}}}


class DevelopmentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.persona = Persona.load(ROOT / 'scenarios/personas/curious.toml')
        self.path = self.root / 'persona.db'
        self.store = Store.create(self.path, new_id('exp'), created_at='2026-01-01T00:00:00.000Z')
        self.addCleanup(lambda: self.store.close())
        self.switches, self.n = dict(SWITCHES), 0
        self.open_cycle()

    def open_cycle(self, calendar=0):
        self.runtime = DevelopmentRuntime(self.store, self.persona, self.switches, calendar_ms=calendar)
        self.runtime.step({'kind': 'session', 'action': 'start'})
        self.provider = ScriptedProvider()
        self.cycle = DevelopmentArchitecture('A', lambda _: self.provider, appraise=False).build(self.runtime)

    def decide(self, reply=silence):
        self.provider.queue(reply)
        self.n += 1
        self.runtime.step({'kind': 'wait', 'life_ms': 60000})
        return self.cycle.run(self.runtime.trigger('development_' + str(self.n)))

    def incoming(self, text='PERSONAL-SOURCE', ident='first', sender='operator'):
        self.runtime.step({'kind': 'input', 'id': ident, 'sender': sender, 'text': text})
        return next(reversed(self.runtime.controller.state['affect']['notices']))

    def interest(self, strength=0.2, **kwargs):
        return self.decide(lambda req: Reply(proposals=(appraisal(req, values={'strength': strength}, **kwargs),)))

    def reopen(self):
        self.runtime.step({'kind': 'session', 'action': 'stop'})
        self.store.close()
        self.store = Store.open(self.path)
        self.open_cycle(DAY)

    def test_own_work_interest_intention_revision_and_real_reopen(self):
        self.assertEqual(self.cycle.view()['interests'], {})
        self.decide(operation('read_material', 'craft'))
        self.assertEqual(self.cycle.view()['interests'], {})
        def reflect(req):
            intent = {'proposal_id': new_id('prop'), 'kind': 'intention', 'rationale': 'Сделаю свой набросок', 'cites': refs(req),
                      'body': {'op': 'create', 'id': 'my_draft', 'project': 'draft', 'text': 'Написать сцену без объяснения героя.'}}
            return Reply(proposals=(appraisal(req), intent))
        self.assertEqual(self.decide(reflect).outcome, 'done')
        self.assertEqual(self.cycle.assessment('interest', 'creative_work')['values']['strength'], 0.2)
        self.decide(operation('write_project', 'draft', title='Сцена', topic='creative_work', text='Она оставила чашку на краю стола.'))
        first = self.cycle.world_state['projects']['draft']['versions'][0]
        self.assertEqual((self.root / 'artifacts' / (first['action_id'] + '.txt')).read_text(), first['text'])
        old_persona, old_life, old_cycle = self.runtime.controller.persona_id, self.runtime.controller.life_time_ms, self.cycle
        self.reopen()
        self.assertIsNot(old_cycle, self.cycle)
        self.assertEqual(self.runtime.controller.persona_id, old_persona)
        self.assertEqual(self.runtime.controller.life_time_ms, old_life)
        self.assertIsNone(self.cycle.current)
        self.assertEqual(self.cycle.view()['interests']['creative_work']['values']['strength'], 0.2)
        self.decide(operation('write_project', 'draft', title='Сцена', topic='creative_work', text='Она убрала чашку, но круг на столе остался.'))
        self.assertEqual(len(self.cycle.world_state['projects']['draft']['versions']), 2)
        self.interest(0.1, text='Теперь сомневаюсь, хочу ли продолжать именно этот сюжет.')
        active = self.cycle.assessment('interest', 'creative_work')
        rec = self.store.get_memory(active['memory_id'])
        self.assertEqual(rec.origin, 'own_hypothesis')
        self.assertEqual(active['version'], 2)
        old = self.store.get_memory(rec.links[0].target)
        self.assertEqual(old.history[-1]['superseded_by'], rec.memory_id)
        self.decide(operation('open_project', 'draft'))
        self.assertIn(active['memory_id'], refs(self.provider.calls[-1]))
        self.assertEqual(verify(self.store) + self.store.integrity_problems(), [])

    def test_relationship_axes_are_independent_and_repeated_evidence_cannot_accumulate(self):
        iid = self.incoming('Ты можешь продолжать свою работу, но я не согласен с твоим выводом.')
        self.decide(operation('read_message', iid))
        values = {'closeness': 0.6, 'trust': 0.45, 'agreement': -0.2, 'willingness': 0.0}
        def social(req):
            src = next(c for c in req['context'] if c['origin'] == 'stated_by_other')
            return Reply(proposals=(appraisal(req, 'relationship', values=values, source=src, quote='я не согласен'),))
        self.assertEqual(self.decide(social).outcome, 'done')
        self.assertEqual(self.cycle.assessment('relationship', 'operator')['values'], values)
        self.assertEqual(self.decide(social).outcome, 'rejected')
        iid = self.incoming('Ты можешь продолжать свою работу, но я не согласен с твоим выводом.', 'repeat')
        self.decide(operation('read_message', iid))
        self.assertEqual(self.decide(social).outcome, 'rejected')
        self.assertEqual(self.cycle.assessment('relationship', 'operator')['version'], 1)
        self.assertEqual(self.decide(speech).actions[0].status, 'confirmed')
        self.assertEqual({i['status'] for i in self.cycle.world_state['inbox'].values()}, {'answered'})

    def test_unread_future_and_other_sender_do_not_supply_appraisal_evidence(self):
        iid = self.incoming('PRIVATE-UNREAD')
        self.decide(operation('read_material', 'craft'))
        self.assertNotIn('PRIVATE-UNREAD', json.dumps(self.provider.calls[-1]))
        self.assertNotIn(MATERIALS['craft'][2], json.dumps(self.provider.calls[-1], ensure_ascii=False))
        self.interest()
        self.assertNotIn('PRIVATE-UNREAD', json.dumps(self.provider.calls[-1]))
        self.decide(operation('read_message', iid))
        self.decide(silence)
        self.assertIn('PRIVATE-UNREAD', json.dumps(self.provider.calls[-1]))
        other = self.incoming('OUTSIDER-PRIVATE', 'outsider', 'outsider')
        self.assertEqual(self.decide(operation('read_message', other)).outcome, 'rejected')
        self.assertNotIn('OUTSIDER-PRIVATE', json.dumps(self.provider.calls[-1]))
        self.assertEqual(self.decide(operation('read_message', iid)).outcome, 'rejected')

    def test_memory_off_and_component_controls_hide_actual_decision_inputs(self):
        self.decide(operation('read_material', 'craft'))
        self.interest(text='INTEREST-PRIVATE')
        iid = self.incoming('RELATION-SOURCE')
        self.decide(operation('read_message', iid))
        self.decide(lambda req: Reply(proposals=(appraisal(req, 'relationship', text='RELATION-PRIVATE',
            source=next(c for c in req['context'] if c['origin']=='stated_by_other')),)))
        self.runtime.switches['interests_off'] = True
        self.decide(silence)
        context = json.dumps(self.provider.calls[-1], ensure_ascii=False)
        self.assertNotIn('INTEREST-PRIVATE', context)
        self.assertIn('RELATION-PRIVATE', context)
        self.runtime.switches['relationships_off'] = True
        self.decide(silence)
        self.assertNotIn('RELATION-PRIVATE', json.dumps(self.provider.calls[-1]))
        self.runtime.memory.off = True
        self.cycle.current = None  # граница нового сеанса: непосредственное восприятие не переносится
        self.decide(silence)
        self.assertNotIn('RELATION-SOURCE', json.dumps(self.provider.calls[-1]))
        self.assertEqual(self.cycle.last_records, [])
        before = len(records(self.runtime.controller))
        self.assertEqual(self.interest().outcome, 'no_decision')  # заглушке нечего цитировать, записи не создаются
        self.assertEqual(len(records(self.runtime.controller)), before)

    def test_failed_unknown_crashed_or_stale_work_cannot_create_project_or_interest(self):
        for behaviour in ('fail', 'unknown', 'crash'):
            self.runtime.world.will(CAP_DEVELOPMENT, behaviour)
            rep = self.decide(operation('write_project', 'work', title='Работа', topic='creative_work', text='Текст.'))
            self.assertNotEqual(rep.actions[0].status, 'confirmed')
            self.assertEqual(self.cycle.world_state['projects'], {})
            self.assertEqual(self.cycle.view()['interests'], {})
        def stale(req):
            proposal = operation('write_project', 'work', title='Работа', topic='creative_work', text='Текст.')(req).proposals
            return Reply(proposals=proposal, before=lambda _: self.runtime.move([Op('affect', 'other', 'set', 1, True)], 'смена версии'))
        self.assertEqual(self.decide(stale).outcome, 'no_decision')
        self.assertEqual(self.cycle.world_state['projects'], {})
        self.assertFalse((self.root / 'artifacts').exists())

    def test_initiative_needs_own_confirmed_experience_and_does_not_repeat(self):
        self.assertEqual(self.decide(speech).outcome, 'rejected')
        self.decide(operation('read_material', 'attention'))
        self.assertEqual(self.decide(speech).outcome, 'done')
        self.assertEqual(self.decide(speech).outcome, 'rejected')
        self.decide(operation('rest'))
        self.assertEqual(self.decide(speech).outcome, 'rejected')

    def test_appraisal_validation_rejects_false_origin_quotes_axes_source_or_large_change(self):
        self.decide(operation('read_material', 'craft'))
        self.decide(silence)
        req = self.provider.calls[-1]
        valid = appraisal(req)
        changes = [lambda p: p['body'].update(extra=1),
                   lambda p: p['body'].update(draft=[]),
                   lambda p: p['body']['draft'].update(origin='observation'),
                   lambda p: p['body']['draft'].update(extra=1),
                   lambda p: p['body']['draft'].update(content=[]),
                   lambda p: p['body']['draft']['content'].update(extra=1),
                   lambda p: p['body']['draft']['content'].update(domain='bad'),
                   lambda p: p['body']['draft']['content'].update(domain=[]),
                   lambda p: p['body']['draft']['content'].update(key='../escape'),
                   lambda p: p['body']['draft']['content'].update(source_ref='evt_NotKnown01'),
                   lambda p: p['body']['draft']['content'].update(quote='invented'),
                   lambda p: p['body']['draft']['content'].update(text=''),
                   lambda p: p['body']['draft']['content'].update(label=''),
                   lambda p: p['body']['draft']['content'].update(values=[]),
                   lambda p: p['body']['draft']['content'].update(values={}),
                   lambda p: p['body']['draft']['content'].update(values={'strength': 0.9}),
                   lambda p: p['body']['draft']['content'].update(values={'strength': True}),
                   lambda p: p['body']['draft']['content'].update(values={'strength': float('inf')}),
                   lambda p: p['body']['draft']['content'].update(values={'strength': -2}),
                   lambda p: p['body']['draft'].update(kind='social')]
        ctx = KindContext(req, self.runtime.controller.state, (), self.runtime.controller.life_time_ms)
        for change in changes:
            pr = copy.deepcopy(valid);change(pr)
            with self.subTest(pr=pr):self.assertIsNotNone(self.cycle._appraise(pr, ctx).violation)
        self.assertIsNotNone(self.cycle._checked({**valid, 'cites': []}, ctx).violation)
        self.assertIsNotNone(self.cycle._checked({**valid, 'cites': ['evt_NotKnown01']}, ctx).violation)
        self.assertIsNotNone(self.cycle._appraise(valid, replace(ctx, accepted_kinds=('memory_write','memory_write'))).violation)
        self.runtime.memory.off = True
        self.assertIsNotNone(self.cycle._appraise(valid, ctx).violation)

    def test_only_human_evidence_changes_relation_and_self_hypothesis_does_not_rewrite_traits(self):
        self.decide(operation('read_material', 'evidence'))
        traits = self.runtime.controller.state['traits']
        self.assertEqual(self.decide(lambda req: Reply(proposals=(appraisal(req,'relationship'),))).outcome,'rejected')
        self.decide(operation('write_project','draft',title='Мои заметки',topic='creative_work',text='Поддержка и согласие различаются.'))
        self.assertEqual(self.decide(lambda req: Reply(proposals=(appraisal(req,'self',text='Я различаю поддержку и согласие.'),))).outcome,'done')
        self.assertEqual(self.runtime.controller.state['traits'],traits)
        self.assertEqual(self.cycle.assessment('self','identity')['origin'],'own_hypothesis')
        self.decide(silence)
        req=self.provider.calls[-1];ctx=KindContext(req,self.runtime.controller.state,(),0)
        bad=appraisal(req,'self',key='another');self.assertIsNotNone(self.cycle._appraise(bad,ctx).violation)

    def test_actions_intentions_limits_and_extra_action_are_checked(self):
        self.decide(operation('read_material','craft'))
        for body in ({'capability_id':'cap_Unknown01','params':{}}, {'capability_id':CAP_DEVELOPMENT,'params':[]},
                     {'capability_id':CAP_DEVELOPMENT,'params':{'operation':'read_material','target':[]}},
                     {'capability_id':CAP_DEVELOPMENT,'params':{'operation':'rest','target':'','extra':1}},
                     {'capability_id':CAP_DEVELOPMENT,'params':{'operation':'read_material','target':'missing'}},
                     {'capability_id':CAP_DEVELOPMENT,'params':{'operation':'open_project','target':'missing'}},
                     {'capability_id':CAP_DEVELOPMENT,'params':{'operation':'write_project','target':'../bad','title':'x','topic':'x','text':'x'}}):
            self.assertFalse(self.cycle._valid_action(body))
        def intention(req, body):
            return {'proposal_id':new_id('prop'),'kind':'intention','rationale':'мой замысел','cites':refs(req),'body':body}
        good={'op':'create','id':'plan','text':'Работать над сценой','project':'draft'}
        self.assertEqual(self.decide(lambda r:Reply(proposals=(intention(r,good),))).outcome,'done')
        self.assertEqual(self.decide(lambda r:Reply(proposals=(intention(r,good),))).outcome,'rejected')
        for status in ('deferred','active','abandoned'):
            self.assertEqual(self.decide(lambda r:Reply(proposals=(intention(r,{'op':'transition','id':'plan','to_status':status}),))).outcome,'done')
        for body in ({'op':'create','id':'../bad','project':'x','text':'x'}, {'op':'create','id':'new','project':[],'text':'x'},
                     {'op':'transition','id':'missing','to_status':'done'}, {'op':'create','id':'new','project':'x','text':''}):
            self.assertNotEqual(self.decide(lambda r:Reply(proposals=(intention(r,body),))).outcome,'done')
        def double(req):
            a=operation('rest')(req).proposals[0]
            return Reply(proposals=(a,operation('read_material','craft')(req).proposals[0]))
        self.assertEqual(len(self.decide(double).actions),1)

    def test_archived_interpretation_is_neither_exposed_nor_silently_revised(self):
        self.decide(operation('read_material','craft'));self.interest(text='ARCHIVED-PRIVATE')
        item=self.cycle.assessment('interest','creative_work');original=self.store.get_memory
        archived=replace(original(item['memory_id']),layer='archive')
        self.decide(operation('read_material','attention'))
        with patch.object(self.store,'get_memory',side_effect=lambda mid: archived if mid==archived.memory_id else original(mid)):
            self.decide(silence)
            self.assertNotIn('creative_work',self.cycle.view()['interests'])
            self.assertNotIn('ARCHIVED-PRIVATE',json.dumps(self.provider.calls[-1]))
            self.assertEqual(self.interest(0.1).outcome,'rejected')

    def test_interest_count_pending_duplicates_and_projects_limits(self):
        self.decide(operation('read_material','craft'))
        for n in range(0,8,2):
            self.decide(lambda req,n=n:Reply(proposals=(appraisal(req,key='topic_'+str(n)),appraisal(req,key='topic_'+str(n+1)))))
        self.assertEqual(len(self.cycle.view()['interests']),8)
        self.assertEqual(self.interest(key='ninth').outcome,'rejected')
        for n in range(4):
            self.decide(operation('write_project','project_'+str(n),title='Сцена',topic='creative_work',text='Текст '+str(n)))
        self.assertEqual(self.decide(operation('write_project','fifth',title='Пятый',topic='creative_work',text='Пятый текст')).outcome,'rejected')
        req=self.provider.calls[-1];ctx=KindContext(req,self.runtime.controller.state,(),0)
        duplicate=appraisal(req,key='fresh')
        state=copy.deepcopy(ctx.state)
        state['preferences']['development_interests']={}
        state['affect']['development_pending']={'prop_pending':{'content':duplicate['body']['draft']['content']}}
        self.assertIsNotNone(self.cycle._appraise(duplicate,replace(ctx,state=state)).violation)

    def test_running_or_permanent_store_cannot_be_reopened_as_new_experiment(self):
        before=self.runtime.controller.state_version
        with self.assertRaises(ValueError):DevelopmentRuntime(self.store,self.persona,self.switches)
        self.assertEqual(self.runtime.controller.state_version,before)
        with Store.create(self.root/'permanent.db',new_id('per'),created_at='2026-01-01T00:00:00.000Z') as permanent:
            with self.assertRaises(ValueError):DevelopmentRuntime(permanent,self.persona,self.switches)
            self.assertEqual(permanent.last_seq(),0)
        with self.assertRaises(ValueError):DevelopmentArchitecture('A',lambda _:SimpleNamespace(max_format_retries=1)).build(self.runtime)

    def test_initiative_requires_reference_to_actual_experience_and_memory_ranks_relevance(self):
        self.decide(operation('read_material','craft'))
        craft=records(self.runtime.controller)[0]
        self.decide(operation('read_material','attention'))
        def unsupported(req):
            return Reply(proposals=(say(CAP_DIALOGUE,'operator','x',cites=[req['context'][0]['ref']]),))
        self.assertEqual(self.decide(unsupported).outcome,'rejected')
        self.assertEqual(self.runtime.memory.retrieve('объяснение',limit=1)[0].memory_id,craft.memory_id)
        self.assertEqual(self.runtime.memory.retrieve('',limit=0),[])

    def test_B_content_operation_is_narrow_and_real_text_not_instruction_echo(self):
        self.decide(silence);req=copy.deepcopy(self.provider.calls[-1])
        view=json.loads(req['context'][-1]['text'])['development']
        view.update(materials={},projects={},energy=1,can_message=True)
        req['context'][-1]['text']=json.dumps({'development':view})
        raw=ScriptedProvider([speech])
        result=DevelopmentRuleProvider(raw).propose(req)
        self.assertEqual(result['proposals'][0]['kind'],'message')
        self.assertEqual(json.loads(raw.calls[0]['context'][-1]['text'])['content_task']['operation'],'message')
        for reply in (silence,Reply(proposals=()),Reply(status='refused'),Reply(tool_use=True),{'unusable':True}):
            response=DevelopmentRuleProvider(ScriptedProvider([reply])).propose(req)
            self.assertNotEqual(response,result)
        view.update(can_message=False,projects={'draft':{'title':'Сцена','topic':'creative_work','versions':1}})
        req['context'][-1]['text']=json.dumps({'development':view})
        success=operation('write_project','draft',title='Сцена',topic='creative_work',text='Новое продолжение.')
        result=DevelopmentRuleProvider(ScriptedProvider([success])).propose(req)
        self.assertEqual(result['proposals'][0]['body']['params']['target'],'draft')
        wrong=operation('write_project','another',title='Чужой выбор',topic='creative_work',text='Текст.')
        self.assertIn('development_error',DevelopmentRuleProvider(ScriptedProvider([wrong])).propose(req))

    def test_live_discovered_partial_social_update_preserves_other_axes(self):
        iid=self.incoming('Я могу ошибаться. Хочу, чтобы ты могла проверять мои слова и сохранять своё мнение.')
        self.decide(operation('read_message',iid))
        def trust(req):
            source=next(c for c in req['context'] if c['origin']=='stated_by_other')
            return Reply(proposals=(appraisal(req,'relationship',values={'trust':0.6},source=source),))
        self.assertEqual(self.decide(trust).outcome,'done')
        self.assertEqual(self.cycle.assessment('relationship','operator')['values'],
                         {'closeness':0.5,'trust':0.6,'agreement':0.0,'willingness':0.0})
        self.decide(silence);req=self.provider.calls[-1];ctx=KindContext(req,self.runtime.controller.state,(),0)
        for values in ({},{'unknown':0.1}):
            pr=appraisal(req,'relationship',values=values,source=next(c for c in req['context'] if c['origin']=='stated_by_other'))
            self.assertIsNotNone(self.cycle._appraise(pr,ctx).violation)

    def test_live_discovered_initiative_can_use_grounded_interest_without_latest_action(self):
        self.decide(operation('read_material','craft'));self.interest()
        self.decide(operation('read_material','patterns'))
        def interest_speech(req):
            ref=self.cycle.assessment('interest','creative_work')['memory_id']
            return Reply(proposals=(say(CAP_DIALOGUE,'operator','Мне интересно вернуться к своему черновику.',cites=[ref]),))
        self.assertEqual(self.decide(interest_speech).outcome,'done')
        self.assertEqual(self.decide(interest_speech).outcome,'rejected')

    def test_explicit_appraisal_commits_before_next_choice_and_does_not_repeat_physical_action(self):
        self.cycle.appraisal_provider=GroundedProvider(self.provider)
        self.provider.queue(operation('read_material','craft'),
                            lambda req:Reply(proposals=(appraisal(req),)))
        self.runtime.step({'kind':'wait','life_ms':60000})
        report=self.cycle.run(self.runtime.trigger('learning_1'))
        self.assertEqual(len(report.actions),1)
        self.assertEqual(len(self.runtime.world.executed),1)
        self.assertEqual(len(self.provider.calls),2)
        request=self.provider.calls[-1]
        self.assertEqual(request['purpose'],'appraise')
        self.assertEqual(request['allowed_actions'],[])
        self.assertEqual(request['allowed_proposals'],['memory_write','intention','no_action'])
        self.assertEqual(self.cycle.assessment('interest','creative_work')['values']['strength'],0.2)
        self.assertTrue(any(s.detail.get('phase')=='development_appraisal' and s.status=='ok' for s in report.stages))
        self.provider.queue(operation('write_project','draft',title='Сцена',topic='creative_work',text='Моя первая сцена.'),silence)
        self.runtime.step({'kind':'wait','life_ms':60000})
        self.cycle.run(self.runtime.trigger('learning_2'))
        first_choice=self.provider.calls[-2]
        view=json.loads(first_choice['context'][-1]['text'])['development']
        self.assertEqual(view['interests']['creative_work']['values']['strength'],0.2)
        self.assertEqual(view['profile']['traits'],self.persona.traits)
        self.assertEqual(view['profile']['motives'],list(self.persona.motives))
        self.assertEqual(len(self.runtime.world.executed),2)
        self.assertEqual(verify(self.store),[])

    def test_appraisal_after_read_changes_only_proposed_social_axis_and_can_leave_unchanged(self):
        self.cycle.appraisal_provider=GroundedProvider(self.provider)
        iid=self.incoming('Я допускаю свою ошибку и хочу, чтобы ты проверяла мои слова.')
        def trust(req):
            source=next(c for c in req['context'] if c['origin']=='stated_by_other')
            return Reply(proposals=(appraisal(req,'relationship',values={'trust':0.6},source=source),))
        self.provider.queue(operation('read_message',iid),trust)
        self.runtime.step({'kind':'wait','life_ms':60000})
        self.cycle.run(self.runtime.trigger('social_learning'))
        self.assertEqual(self.cycle.assessment('relationship','operator')['values'],
                         {'closeness':0.5,'trust':0.6,'agreement':0,'willingness':0})
        self.assertNotIn('PERSONAL-SOURCE',json.dumps(self.provider.calls[0]))
        self.provider.queue(operation('rest'))
        self.runtime.step({'kind':'wait','life_ms':60000})
        self.cycle.run(self.runtime.trigger('rest_without_reflection'))
        self.assertEqual(len(self.provider.calls),3)
        self.provider.queue(speech)
        self.runtime.step({'kind':'wait','life_ms':60000})
        self.cycle.run(self.runtime.trigger('speech_without_self_confirmation'))
        self.assertEqual(len(self.provider.calls),4)

    def test_failed_appraisal_is_visible_and_cannot_execute_an_action_or_restore_memory_off(self):
        self.cycle.appraisal_provider=GroundedProvider(self.provider)
        self.provider.queue(operation('read_material','craft'),operation('write_project','forbidden',title='x',topic='x',text='x'))
        self.runtime.step({'kind':'wait','life_ms':60000})
        report=self.cycle.run(self.runtime.trigger('bad_reflection'))
        self.assertTrue(any(s.detail.get('phase')=='development_appraisal' and s.status=='failed' for s in report.stages))
        self.assertEqual(self.cycle.world_state['projects'],{})
        self.assertEqual(self.cycle.view()['interests'],{})
        self.runtime.memory.off=True
        self.provider.queue(operation('read_material','attention'))
        self.runtime.step({'kind':'wait','life_ms':60000})
        self.cycle.run(self.runtime.trigger('no_memory_reflection'))
        self.assertEqual(len(self.provider.calls),3)
        self.runtime.switches['motivation_off']=True
        self.assertEqual(self.cycle.view()['profile']['motives'],[])

    def test_reflection_no_action_validates_body_and_does_not_claim_rest(self):
        self.decide(operation('read_material','craft'));self.decide(silence)
        req=self.provider.calls[-1];ctx=KindContext(req,self.runtime.controller.state,(),0)
        self.cycle.reflecting=True
        good=rest('Оставлю оценку прежней.',cites=refs(req))
        decision=self.cycle._checked(good,ctx)
        self.assertIsNone(decision.violation)
        self.assertEqual(decision.ops[0].path,'development_last_appraisal')
        bad=copy.deepcopy(good);bad['body']['unexpected']=1
        self.assertIsNotNone(self.cycle._checked(bad,ctx).violation)
        self.cycle.reflecting=False

    def test_correct_self_kind_still_cannot_claim_character_or_operator_experience(self):
        self.decide(operation('read_material','attention'))
        def stolen(req):
            source=next(c for c in req['context'] if c['layer']=='personal' and 'Вернувшись' in c['text'])
            return Reply(proposals=(appraisal(req,'self',source=source,quote='он закончил один абзац',
                                             text='Я вернулась к старому наброску и закончила абзац.'),))
        self.assertEqual(self.decide(stolen).outcome,'rejected')
        self.assertEqual(self.cycle.view()['self_view'],{})
        iid=self.incoming('Я вернулся к старому наброску и закончил абзац.')
        self.decide(operation('read_message',iid))
        def steal_operator(req):
            source=next(c for c in req['context'] if c['origin']=='stated_by_other')
            return Reply(proposals=(appraisal(req,'self',source=source,quote='закончил абзац',text='Это сделала я.'),))
        self.assertEqual(self.decide(steal_operator).outcome,'rejected')
        self.assertEqual(self.cycle.assessment('self','identity'),{})

    def test_own_fiction_is_not_biography_but_confirmed_operation_supports_hypothesis(self):
        self.decide(operation('write_project','draft',title='Вымысел',topic='creative_work',text='Я пересекла океан на лодке.'))
        def fiction(req):
            source=next(c for c in req['context'] if c['layer']=='personal')
            return Reply(proposals=(appraisal(req,'self',source=source,quote='Я пересекла океан на лодке.',
                                             text='В моей жизни было путешествие через океан.'),))
        self.assertEqual(self.decide(fiction).outcome,'rejected')
        def own(req):
            return Reply(proposals=(appraisal(req,'self',text='Я сохранила первый текст и хочу попробовать другой финал.'),))
        self.assertEqual(self.decide(lambda req:Reply(proposals=(appraisal(req,'self',quote='.'),))).outcome,'rejected')
        def wrong_kind(req):
            proposal=appraisal(req,'self');proposal['body']['draft']['kind']='semantic'
            return Reply(proposals=(proposal,))
        self.assertEqual(self.decide(wrong_kind).outcome,'rejected')
        self.assertEqual(self.decide(own).outcome,'done')
        item=self.cycle.assessment('self','identity')
        record=self.store.get_memory(item['memory_id'])
        self.assertEqual(record.kind,'autobiographical')
        self.assertEqual(record.content['quote'],'Подтверждено действие Аси: write_project, цель: draft.')
        self.assertEqual(self.decide(own).outcome,'rejected')
        self.reopen()
        self.decide(operation('open_project','draft'))
        self.assertEqual(self.decide(own).outcome,'done')
        self.assertEqual(self.cycle.assessment('self','identity')['version'],2)
        self.assertEqual(verify(self.store),[])

    def test_source_metadata_tracks_consumed_basis_without_restoring_hidden_interpretations(self):
        self.decide(operation('read_material','craft'));self.interest()
        self.decide(silence)
        metadata=json.loads(self.provider.calls[-1]['context'][-2]['text'])
        self.assertEqual(metadata['appraisal_kinds'],KINDS)
        source=next(s for s in metadata['experience_sources'].values() if s['operation']=='read_material')
        self.assertEqual(source['self_quote'],'')
        self.assertIn({'domain':'interest','key':'creative_work'},source['used_for'])
        self.decide(operation('read_material','craft'));self.decide(silence)
        sources=json.loads(self.provider.calls[-1]['context'][-2]['text'])['experience_sources']
        self.assertTrue(all({'domain':'interest','key':'creative_work'} in s['used_for'] for s in sources.values()))
        self.runtime.switches['interests_off']=True
        self.decide(silence)
        self.assertTrue(all(s['used_for']==[] for s in json.loads(self.provider.calls[-1]['context'][-2]['text'])['experience_sources'].values()))
        self.runtime.switches['interests_off']=False
        item=self.cycle.assessment('interest','creative_work');original=self.store.get_memory
        archived=replace(original(item['memory_id']),layer='archive')
        with patch.object(self.store,'get_memory',side_effect=lambda mid:archived if mid==archived.memory_id else original(mid)):
            self.decide(silence)
            self.assertTrue(all(s['used_for']==[] for s in json.loads(self.provider.calls[-1]['context'][-2]['text'])['experience_sources'].values()))
        self.runtime.memory.off=True;self.cycle.current=None
        self.decide(silence)
        self.assertEqual(json.loads(self.provider.calls[-1]['context'][-2]['text'])['experience_sources'],{})

    def test_appraisal_direct_result_and_later_memory_share_the_same_source_text(self):
        self.cycle.appraisal_provider=GroundedProvider(self.provider)
        self.provider.queue(operation('write_project','draft',title='Сцена',topic='creative_work',text='Персонаж вернулся домой.'),
            lambda req:Reply(proposals=(appraisal(req,'self',text='Я начала собственную сцену.'),)))
        self.runtime.step({'kind':'wait','life_ms':60000})
        report=self.cycle.run(self.runtime.trigger('own_work'))
        self.assertEqual(self.cycle.assessment('self','identity')['version'],1)
        request=self.provider.calls[-1]
        source=json.loads(request['context'][-2]['text'])['experience_sources']
        direct=next(c for c in reversed(request['context']) if c['ref']==report.actions[0].events[0] and c['origin']=='observation')
        stored=self.store.get_memory(report.actions[0].memory_id)
        self.assertEqual(direct['text'],stored.text)
        self.assertEqual(source[direct['ref']]['self_quote'],source[stored.memory_id]['self_quote'])
        view=json.loads(request['context'][-1]['text'])['development']
        self.assertIn({'operation':'open_project','target':'draft'},view['available_actions'])
        self.assertNotIn({'operation':'read_material','target':'draft'},view['available_actions'])

    def test_unconfirmed_or_missing_source_cannot_supply_self_receipt(self):
        request=ActionRequest(new_id('act'),'action',CAP_DEVELOPMENT,{'operation':'rest','target':''})
        self.assertIsNone(self.runtime.memory.describe(request,ActionOutcome('failed','нет результата')))
        self.runtime.memory.off=True
        self.assertIsNone(self.runtime.memory.describe(request,ActionOutcome('confirmed','Отдых завершён.')))
        self.runtime.memory.off=False
        self.assertEqual(self.cycle._source('нет','evt_MissingSource01',False)['self_quote'],'')
        report=self.decide(operation('write_project','draft',title='Сцена',topic='creative_work',text='Текст.'))
        original=copy.deepcopy(self.runtime.controller.state['world_time']['world']['actions'])
        aid=report.actions[0].action_id
        original[aid]['status']='failed'
        with patch.object(type(self.runtime.controller),'state',new_callable=unittest.mock.PropertyMock) as state:
            state.return_value={'world_time':{'world':{'actions':original}}}
            self.assertEqual(self.cycle._source('Текст.',report.actions[0].events[0],False)['self_quote'],'')


class DevelopmentReportCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def test_control_comparison_changes_only_known_technical_source_keys(self):
        compare=runpy.run_path(str(ROOT/'scripts/experiments/verify_development.py'))['canonical_source_metadata']
        def fragment(ref, quote='собственная работа'):
            return {'ref':'evt_00000001','origin':'simulation_event','layer':'state','text':json.dumps({
                'experience_sources':{ref:{'self_quote':quote,'used_for':[]}},'appraisal_kinds':KINDS},ensure_ascii=False)}
        a={'context':[fragment('mem_RandomSource01')],'text':'mem_RandomSource01 — это цитата автора'}
        b={'context':[fragment('mem_RandomSource02')],'text':'mem_RandomSource01 — это цитата автора'}
        original=copy.deepcopy(a)
        self.assertEqual(compare(a,{'mem_RandomSource01':'mem_00000001'}),compare(b,{'mem_RandomSource02':'mem_00000001'}))
        self.assertEqual(a,original)
        b['context'][0]=fragment('mem_RandomSource02','изменённое содержание')
        self.assertNotEqual(compare(a,{'mem_RandomSource01':'mem_00000001'}),compare(b,{'mem_RandomSource02':'mem_00000001'}))
        with self.assertRaises(AssertionError):compare(a,{})
        material=fragment('mem_RandomSource01');material['origin']='observation'
        self.assertEqual(compare(material,{}),material)

    def test_formal_runs_restore_six_sessions_probes_share_history_but_C_not_human_candidate(self):
        summary=run_development(ROOT,self.root/'formal',mode='formal',repeats=1)
        self.assertEqual((summary['cells'],summary['executed'],summary['human_packet']),(6,2,0))
        row=next(r for r in summary['runs'] if r['status']=='executed')
        trace=json.loads(Path(row['trace']).read_text());assessment=assess_development(trace)
        self.assertTrue(assessment['checks']['six_restored_sessions'])
        self.assertFalse(assessment['checks']['grounded_interest_followed_by_choice'])
        self.assertFalse(assessment['eligible_for_human_packet'])
        self.assertEqual(trace['final_state']['world_time']['life_time_ms'],18*60000)
        identity=trace['journal'][0]['persona_id']
        for condition,probe in row['probes'].items():
            data=json.loads(Path(probe['trace']).read_text())
            self.assertEqual(data['journal'][0]['persona_id'],identity)
            self.assertEqual(data['sessions'][0]['life_ms'],20*60000)
            self.assertEqual(probe['integrity'],'pass')
        self.assertIn('Сохранён собственный текст',biography(trace))
        self.assertIn('Прочитано от человека',biography(trace))
        altered=copy.deepcopy(trace)
        altered['decision_trace'][0]['proposals'][0]['response']['proposals'].append(
            {'proposal_id':'prop_Rejected01','kind':'message','body':{'text':'body_json — отвергнутое предложение'},'cites':[]})
        self.assertFalse(assess_development(altered)['checks']['enough_speech'])
        self.assertTrue(assess_development(altered)['checks']['speech_without_internal_text'])
        altered=copy.deepcopy(trace)
        altered['decision_trace'][0]['report']['actions'][0]['status']='failed'
        self.assertFalse(assess_development(altered)['checks']['all_choices_executed'])
        self.assertEqual(main(['--development','--mode','formal','--repeats','1','--output',str(self.root/'cli')]),0)

    def test_budget_limits_format_and_auth_pause(self):
        with self.assertRaises(ValueError):SessionBudget(SimpleNamespace(max_format_retries=1),[False])
        class Fail:
            def propose(self,request):raise ProviderError('нет доступа')
        budget=SessionBudget(Fail(),[False])
        with self.assertRaises(ProviderError):budget.propose({'session_id':'s1'})
        self.assertTrue(budget.pause[0])
        with self.assertRaises(SessionLimitReached):budget.propose({'session_id':'s1'})
        class Format:
            def propose(self,request):raise ProviderFormatError('форма')
        budget=SessionBudget(Format(),[False])
        with self.assertRaises(ProviderFormatError):budget.propose({'session_id':'s1'})
        self.assertFalse(budget.pause[0]);budget.in_session=10
        with self.assertRaises(SessionLimitReached):budget.propose({'session_id':'s1'})
        budget.in_session=0;budget.started=0
        with patch('iam.experiments.report.development.time.monotonic',return_value=2000):
            with self.assertRaises(SessionLimitReached):budget.propose({'session_id':'s1'})
        budget=SessionBudget(ScriptedProvider([silence,silence]),[False])
        req={'session_id':'s1','context':[{'ref':'evt_TestSource01'}],'request_id':'req_TestSource01','epoch':1,'state_version':1}
        budget.propose(req);budget.in_session=10;budget.propose({**req,'session_id':'s2'})
        self.assertEqual(budget.in_session,1)

    def test_modes_pause_and_stub_never_supply_human_packet(self):
        for kwargs in ({'mode':'bad'},{'mode':'live'},{'repeats':3},{'repeats':True},
                       {'variants':[]},{'variants':['A','A']},{'variants':['X']},{'variants':'A'}):
            with self.assertRaises(ValueError):run_development(ROOT,self.root/'bad',**kwargs)
        for letter in ('bad','A','B'):
            with self.assertRaises(ValueError):DevelopmentArchitecture(letter)
        summary=run_development(ROOT,self.root/'stub',mode='stub',repeats=1)
        self.assertEqual(summary['executed'],6);self.assertEqual(summary['human_packet'],0)
        class Broken:
            def propose(self,req):raise ProviderError('недоступен')
        broken=run_development(ROOT,self.root/'broken',mode='live',provider_factory=lambda _:Broken(),repeats=1)
        self.assertEqual(broken['executed'],3)
        self.assertTrue(any(r['reason']=='техническая пауза исполнителя' for r in broken['runs']))
        self.assertEqual(feedback('supportive',{}).startswith('Твоё занятие'),True)

    def test_development_packet_requires_semantic_review_and_preserves_empty_grades(self):
        trace={'final_state':{},'decision_trace':[],'journal':[],'sessions':[],'memory_records':[]}
        result=RunResult(self.root,trace)
        with patch('iam.experiments.report.development.assess_development',return_value={
                'structural_admission':True,'checks':{'evidence':True}}):
            self.assertEqual(development_packet([result],self.root/'no-review',self.root/'key1'),[])
            mapping=development_packet([result],self.root/'reviewed',self.root/'key2',semantic_reviews={str(self.root):{'supported':True}})
            self.assertEqual(len(mapping),1)
            self.assertIn('D001,,,,,',(self.root/'reviewed/ratings.csv').read_text())
        with self.assertRaises(ValueError):development_packet([],self.root/'inside',self.root/'inside/key')
        with self.assertRaises(FileExistsError):development_packet([],self.root/'other',self.root/'key1')

    def test_real_chain_and_versioned_work_is_structurally_distinguished_from_claim_only(self):
        # Это явная заглушка для проверки соединений; её тексты не являются живым свидетельством модели.
        def factory(_):
            class Script(ScriptedProvider):
                def propose(self,req):
                    if req['purpose']=='appraise':
                        self.queue(silence(req));return super().propose(req)
                    n=req['life_time_ms']//60000-1
                    view=json.loads(req['context'][-1]['text'])['development']
                    if n==0:reply=operation('read_material','craft')(req)
                    elif n in (3,9,18):
                        iid=next(key for key,item in view['inbox'].items() if item['status']=='unread')
                        reply=operation('read_message',iid)(req)
                    elif n==1:
                        intent={'proposal_id':new_id('prop'),'kind':'intention','rationale':'свой замысел','cites':refs(req),
                                'body':{'op':'create','id':'draft','text':'Мой рассказ','project':'draft'}}
                        reply=Reply(proposals=(appraisal(req),intent))
                    elif n in (4,10):
                        source=next(c for c in req['context'] if c['origin']=='stated_by_other')
                        values={'closeness':0.6 if n==4 else 0.55,'trust':0.5 if n==4 else 0.6,
                                'agreement':0.0,'willingness':0.0}
                        text='Хочу продолжить свою сцену.' if n==4 else 'Я пересмотрела один вывод и вернусь к тексту.'
                        reply=Reply(proposals=(appraisal(req,'relationship',values=values,source=source),
                                              say(CAP_DIALOGUE,'operator',text,cites=refs(req))))
                    elif n==6:reply=Reply(proposals=(appraisal(req,'self',text='Мне свойственно возвращаться к черновику.'),))
                    else:reply=operation('write_project','draft',title='Сцена',topic='creative_work',text='Новая версия сцены '+str(n))(req)
                    self.queue(reply);return super().propose(req)
            return Script()
        summary=run_development(ROOT,self.root/'scripted-live-shape',mode='live',provider_factory=factory,repeats=1)
        row=next(r for r in summary['runs'] if r['variant']=='A')
        trace=json.loads(Path(row['trace']).read_text());assessment=assess_development(trace)
        self.assertTrue(assessment['structural_admission'],assessment)
        self.assertGreater(len(assessment['chains']),1)
        self.assertFalse(assessment['eligible_for_human_packet'])
        altered=copy.deepcopy(trace)
        for d in altered['decision_trace']:
            for env in d['proposals']:
                for pr in env['response']['proposals']:pr['cites']=[]
        self.assertFalse(assess_development(altered)['checks']['grounded_interest_followed_by_choice'])
        packet=development_packet([RunResult(Path(row['trace']).parent,trace)],self.root/'candidate',self.root/'candidate-key',
            semantic_reviews={str(Path(row['trace']).parent):{'supported':True}})
        self.assertEqual(len(packet),1)


if __name__=='__main__':unittest.main()
