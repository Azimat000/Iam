"""Цели и критерии в этих тестах назначены заглушкой, не являются живой мотивацией."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

import experiments.test_development as support
from iam.experiments.commitment import CommitmentArchitecture, INSTRUCTION
from iam.experiments.conversation import records
from iam.experiments.development import CAP_DEVELOPMENT, DevelopmentRuntime
from iam.experiments.volition import VolitionArchitecture
from iam.state import verify
from iam.stubs import Reply, ScriptedProvider


class CommitmentTests(unittest.TestCase):
    setUp=support.DevelopmentCase.setUp
    decide=support.DevelopmentCase.decide
    reopen=support.DevelopmentCase.reopen

    def open_cycle(self,calendar=0):
        self.runtime=DevelopmentRuntime(self.store,self.persona,self.switches,calendar_ms=calendar)
        self.runtime.step({'kind':'session','action':'start'})
        self.provider=ScriptedProvider()
        self.cycle=CommitmentArchitecture(lambda _:self.provider).build(self.runtime)

    def goal(self,operation='write_project',target='draft',**updates):
        body={'op':'create','id':'later','text':'Проверить выбранный будущий результат.',
              'operation':operation,'target':target,'criterion':'В результате записано наблюдение и отдельный вывод.'}
        body.update(updates);return support.intention(body)

    def capture(self):
        out=[];self.decide(lambda q:(out.append(q),support.silence(q))[1]);return out[0]

    def write(self,text='Наблюдение: число 15. Вывод пока не проверен.'):
        return self.decide(support.operation('write_project','draft',title='Заметки',topic='investigation',text=text))

    def done(self,q,quote=None,**updates):
        sources=json.loads(q['context'][-2]['text'])['experience_sources']
        ref=next(ref for ref,item in sources.items() if 'later' in item['completion_for'])
        body={'op':'transition','id':'later','to_status':'done','result_ref':ref,'result_quote':quote or 'Вывод пока не проверен.'}
        body.update(updates);return support.intention(body)(q)

    def test_write_goal_criterion_survives_restart_and_has_exact_later_result(self):
        self.write();self.decide(self.goal());self.reopen();q=self.capture()
        view=json.loads(q['context'][-1]['text']);g=view['development']['intentions']['later']
        self.assertEqual(g['criterion'],'В результате записано наблюдение и отдельный вывод.')
        self.assertNotIn('project',g);self.assertEqual(g['operation'],'write_project')
        self.assertTrue(any(g['criterion'] in c['text'] for c in q['context'] if c['origin']=='own_hypothesis'))
        self.assertEqual(q['context'][0]['text'],INSTRUCTION)
        self.write();self.assertEqual(self.decide(self.done).outcome,'done')
        saved=self.runtime.controller.state['intentions']['later'];self.assertEqual(saved['status'],'done')
        self.assertEqual(saved['result_quote'],'Вывод пока не проверен.');self.assertFalse(verify(self.store))

    def test_read_goal_can_complete_and_uses_same_goal_formula(self):
        self.decide(support.operation('read_material','patterns'))
        self.decide(self.goal('read_material','patterns',criterion='Прочитан доступный материал о гипотезах.'))
        q=self.capture();item=next(x for x in json.loads(q['context'][-1]['text'])['motivation']['items'] if x['activity']=='material:patterns')
        self.assertEqual(item['accepted_goals'],['later'])
        self.assertEqual(item['salience'],self.cycle.mechanism.score(item['inputs'],0.6,True))
        self.decide(support.operation('read_material','patterns'))
        self.assertEqual(self.decide(lambda q:self.done(q,'Разности растут на единицу.')).outcome,'done')

    def test_open_goal_does_not_accept_wrong_operation_or_earlier_result(self):
        self.write();self.decide(self.goal('open_project'))
        q=self.capture();self.assertFalse(any(x['completion_for'] for x in json.loads(q['context'][-2]['text'])['experience_sources'].values()))
        self.write();q=self.capture();self.assertFalse(any(x['completion_for'] for x in json.loads(q['context'][-2]['text'])['experience_sources'].values()))
        self.decide(support.operation('open_project','draft'));self.assertEqual(self.decide(self.done).outcome,'done')

    def test_wrong_target_unavailable_quote_and_criterion_changes_are_rejected(self):
        self.write();self.decide(self.goal())
        self.decide(support.operation('write_project','other',title='Другой',topic='x',text='Другой результат.'))
        q=self.capture();self.assertFalse(any(x['completion_for'] for x in json.loads(q['context'][-2]['text'])['experience_sources'].values()))
        self.write()
        for update in ({'result_quote':'Несуществующая цитата.'},{'result_quote':''},{'extra':'x'},{'result_ref':[]},{'result_ref':'evt_UnknownRef001'}):
            self.assertEqual(self.decide(lambda q,u=update:self.done(q,**u)).outcome,'rejected')
        self.assertEqual(self.decide(support.intention({'op':'transition','id':'later','to_status':'deferred','criterion':'Новый критерий'})).outcome,'rejected')
        self.assertEqual(self.decide(support.intention({'op':'transition','id':'later','to_status':'deferred'})).outcome,'done')
        q=self.capture();self.assertFalse(any(x['completion_for'] for x in json.loads(q['context'][-2]['text'])['experience_sources'].values()))

    def test_invalid_create_cannot_assign_goal_and_missing_goal_is_valid(self):
        self.assertFalse(self.runtime.controller.state['intentions']);self.capture()
        self.write()
        for update in ({'criterion':''},{'criterion':7},{'operation':'rest'},{'target':[]},{'id':[]},{'operation':'read_material','target':'missing'},{'extra':'x'}):
            self.assertEqual(self.decide(self.goal(**update)).outcome,'rejected')
        self.runtime.memory.off=True;self.assertEqual(self.decide(self.goal()).outcome,'rejected')
        self.assertFalse(self.runtime.controller.state['intentions'])

    def test_legacy_goal_is_read_without_invented_criterion(self):
        self.write();self.cycle=VolitionArchitecture(lambda _:self.provider).build(self.runtime)
        self.decide(support.intention({'op':'create','id':'later','project':'draft','text':'Сохранить новый вывод.'}))
        self.cycle=CommitmentArchitecture(lambda _:self.provider).build(self.runtime)
        q=self.capture();g=json.loads(q['context'][-1]['text'])['development']['intentions']['later']
        self.assertIsNone(g['criterion']);self.assertFalse(g['criterion_recorded'])
        self.write()
        def legacy(q):
            p=self.done(q).proposals[0];p['body'].pop('result_quote');return Reply(proposals=(p,))
        self.assertEqual(self.decide(legacy).outcome,'done')

    def test_shortened_or_archived_source_cannot_supply_completion_or_quote(self):
        self.write();self.decide(self.goal());report=self.write();mid=report.actions[0].memory_id
        original=self.store.get_memory;record=original(mid)
        for replacement in (replace(record,layer='archive'),replace(record,content={'text':'Помню занятие.'})):
            with patch.object(self.store,'get_memory',side_effect=lambda key:replacement if key==mid else original(key)):
                q=self.capture();self.assertFalse(any(x['completion_for'] for x in json.loads(q['context'][-2]['text'])['experience_sources'].values()))
        self.cycle.memory.off=True;self.assertFalse(json.loads(self.capture()['context'][-1]['text'])['motivation']['enabled'])

    def test_mixed_write_and_create_remain_optional_and_bounded(self):
        self.write()
        def choice(q):
            return Reply(proposals=(self.goal()(q).proposals[0],support.operation('rest')(q).proposals[0]))
        self.assertEqual(self.decide(choice).actions[0].status,'confirmed')
        self.assertEqual(self.runtime.controller.state['intentions']['later']['status'],'active')

    def test_future_target_has_goal_component_before_any_result_without_invented_history(self):
        self.write();self.decide(self.goal('read_material','craft'))
        q=self.capture();signal=json.loads(q['context'][-1]['text'])['motivation']
        self.assertFalse(any(i['activity']=='material:craft' for i in signal['items']))
        item=signal['goal_opportunities'][0]
        self.assertEqual(item,{'id':'later','operation':'read_material','target':'craft','basis_available':True,
                               'available':True,'activity_history_present':False,'goal_component':0.45,
                               'component_in_historical_salience':False,'informational_only':True,
                               'component_scope':'activity_boolean_not_per_goal'})
        self.decide(support.intention({'op':'transition','id':'later','to_status':'deferred'}))
        self.assertFalse(json.loads(self.capture()['context'][-1]['text'])['motivation']['goal_opportunities'])

    def test_unavailable_future_project_and_invisible_basis_have_no_goal_component(self):
        self.write();self.decide(self.goal('write_project','future'))
        for key in ('one','two','three'):
            self.decide(support.operation('write_project',key,title='Текст',topic='x',text='Короткая запись.'))
        q=self.capture();item=json.loads(q['context'][-1]['text'])['motivation']['goal_opportunities'][0]
        self.assertFalse(item['available']);self.assertEqual(item['goal_component'],0.0)
        original=self.store.get_memory;hidden={r.memory_id:replace(r,layer='archive') for r in records(self.runtime.controller)}
        with patch.object(self.store,'get_memory',side_effect=lambda mid:hidden.get(mid) or original(mid)):
            item=json.loads(self.capture()['context'][-1]['text'])['motivation']['goal_opportunities'][0]
            self.assertFalse(item['basis_available']);self.assertEqual(item['goal_component'],0.0)

    def test_receipt_text_does_not_count_as_result_quote(self):
        self.write();self.decide(self.goal());self.write()
        self.assertEqual(self.decide(lambda q:self.done(q,'Подтверждено действие Аси: write_project, цель: draft.')).outcome,'rejected')

    def test_open_only_basis_is_available_without_scored_open_history(self):
        self.write();opened=self.decide(support.operation('open_project','draft')).actions[0].memory_id
        self.decide(support.intention({'op':'create','id':'later','text':'Прочитать материал.','operation':'read_material',
            'target':'craft','criterion':'Получен текст материала.'},cites=lambda q:[opened]))
        item=json.loads(self.capture()['context'][-1]['text'])['motivation']['goal_opportunities'][0]
        self.assertTrue(item['basis_available']);self.assertEqual(item['goal_component'],0.45)

    def test_unavailable_capability_removes_component_and_two_goals_do_not_double_salience(self):
        self.write();self.decide(self.goal())
        self.decide(self.goal(id='second'))
        signal=json.loads(self.capture()['context'][-1]['text'])['motivation'];item=signal['items'][0]
        self.assertEqual(set(item['accepted_goals']),{'later','second'})
        self.assertEqual(item['salience'],self.cycle.mechanism.score(item['inputs'],0.6,True))
        self.assertTrue(all(x['component_in_historical_salience'] and x['informational_only'] for x in signal['goal_opportunities']))
        self.runtime.capabilities.set_status(CAP_DEVELOPMENT,'revoked')
        signal=json.loads(self.capture()['context'][-1]['text'])['motivation']
        self.assertTrue(all(not x['available'] and x['goal_component']==0 for x in signal['goal_opportunities']))
        self.assertFalse(signal['items'][0]['accepted_goals'])
        with patch.dict(self.runtime.capabilities._caps,{},clear=True):
            self.assertFalse(json.loads(self.capture()['context'][-1]['text'])['motivation']['goal_opportunities'][0]['available'])
