"""Происхождение — метаданные проверяющего стенда, не подсказка требуемого желания.

Все ответы, в том числе имитация конверта codex, — локальные тестовые заглушки.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import experiments.test_development as support
from iam.domain import new_id
from iam.experiments.conversation import records
from iam.experiments.development import CAP_DEVELOPMENT, DevelopmentRuntime
from iam.experiments.provenance import (
    REGISTRY, ProvenanceArchitecture, content_origin, projected_text, seed_history, selection,
)
from iam.experiments.report.development import fork_checkpoint, save_trace
from iam.experiments.transfer import TransferArchitecture, TransferCycle
from iam.state import verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider


class ProvenanceTests(unittest.TestCase):
    setUp = support.DevelopmentCase.setUp
    decide = support.DevelopmentCase.decide
    incoming = support.DevelopmentCase.incoming
    reopen = support.DevelopmentCase.reopen

    def open_cycle(self, calendar=0):
        self.runtime = DevelopmentRuntime(self.store,self.persona,self.switches,calendar_ms=calendar)
        self.runtime.step({'kind':'session','action':'start'})
        self.provider = ScriptedProvider(name='codex',model='gpt-6-luna')
        self.cycle = ProvenanceArchitecture(lambda _:self.provider).build(self.runtime)

    def write(self,text='Собственная версия: она открыла дверь.'):
        return self.decide(support.operation('write_project','draft',title='У двери',topic='creative_work',text=text))

    def capture(self):
        got=[]
        self.decide(lambda req:(got.append(req),support.silence(req))[1])
        return got[0]

    def fixture(self,live_writes=2,reading=False):
        directory=self.root/'past';directory.mkdir()
        with Store.create(directory/'persona.db',new_id('exp')) as store:
            runtime=DevelopmentRuntime(store,self.persona,self.switches)
            runtime.step({'kind':'session','action':'start'})
            p=ScriptedProvider();architecture=TransferArchitecture(lambda _:p);cycle=architecture.build(runtime)
            for n in range(1+live_writes):
                if n:p.name,p.model='codex','gpt-6-luna'
                p.queue(support.operation('write_project','draft',title='У двери',topic='creative_work',text='PREPARED-PRIVATE' if n==0 else 'LIVE-VERSION-'+str(n)))
                runtime.step({'kind':'wait','life_ms':60000});cycle.run(runtime.trigger('past_'+str(n)))
                if n==0:cutoff=store.last_seq()
            if reading:
                p.queue(support.operation('read_material','craft'))
                runtime.step({'kind':'wait','life_ms':60000});cycle.run(runtime.trigger('past_read'))
            runtime.step({'kind':'session','action':'stop'})
        result,problems=save_trace(directory,architecture,self.persona,[],[],self.switches)
        self.assertFalse(problems)
        return directory,hashlib.sha256(result.trace_bytes).hexdigest(),cutoff

    def copy_fixture(self,live_writes=2,seed=True,build=True,reading=False):
        directory,sha,cutoff=self.fixture(live_writes,reading)
        self.store.close();target=self.root/'copy';fork_checkpoint(directory,target)
        self.path=target/'persona.db';self.store=Store.open(self.path)
        self.runtime=DevelopmentRuntime(self.store,self.persona,self.switches)
        self.runtime.step({'kind':'session','action':'start'})
        if seed:seed_history(self.runtime,directory/'trace.json',sha,cutoff)
        self.provider=ScriptedProvider(name='codex',model='gpt-6-luna')
        if build:self.cycle=ProvenanceArchitecture(lambda _:self.provider).build(self.runtime)
        return directory,sha,cutoff

    def test_preparation_and_later_model_versions_have_distinct_visible_receipts(self):
        self.copy_fixture()
        req=self.capture();value=json.loads(req['context'][-1]['text'])
        items=value['experience_provenance'];self.assertEqual(len(items),3)
        self.assertEqual(sum(x['selection']=='prepared_setup' for x in items.values()),1)
        self.assertEqual(sum(x['selection']=='model_live' for x in items.values()),2)
        for ref,item in items.items():
            text=next(c['text'] for c in req['context'] if c['ref']==ref)
            self.assertTrue(text.startswith('Подготовлено стендом:' if item['selection']=='prepared_setup' else 'Подтверждено действие Аси:'))
        self.assertNotIn('Личная история начинается здесь',req['context'][0]['text'])
        self.assertNotIn('Подготовленное прошлое этой короткой проверки постановочное',req['context'][0]['text'])
        self.assertEqual(value['development']['work_continuity']['draft']['visible_versions'],3)
        old=next(r for r in records(self.runtime.controller) if 'PREPARED-PRIVATE' in r.text)
        self.assertTrue(old.text.startswith('Подтверждено действие Аси:'))  # Историю не переписываем.
        self.assertFalse(verify(self.store))

    def test_opening_prepared_version_is_own_reading_without_claiming_authorship(self):
        self.copy_fixture(0)
        self.decide(support.operation('open_project','draft'))
        req=self.capture();v=json.loads(req['context'][-1]['text'])
        opening=next((ref,item) for ref,item in v['experience_provenance'].items() if item.get('operation')=='open_project')
        ref,item=opening;self.assertEqual(item['selection'],'model_live');self.assertEqual(item['content_origin'],'prepared_version')
        text=next(c['text'] for c in req['context'] if c['ref']==ref)
        self.assertIn('Содержание доступной версии;',text)
        self.assertIn('PREPARED-PRIVATE',text)
        self.assertTrue(json.loads(req['context'][-2]['text'])['experience_sources'][ref]['self_quote'])
        self.write();self.decide(support.operation('open_project','draft'))
        req=self.capture();v=json.loads(req['context'][-1]['text'])
        self.assertTrue(any(i.get('operation')=='open_project' and i['content_origin']=='model_written_version' for i in v['experience_provenance'].values()))

    def test_prepared_action_cannot_become_own_autobiographical_action(self):
        self.copy_fixture(0);req=self.capture()
        ref=next(iter(json.loads(req['context'][-1]['text'])['experience_provenance']))
        source=next(c for c in req['context'] if c['ref']==ref)
        quote='Подтверждено действие Аси: write_project, цель: draft.'
        report=self.decide(lambda req:Reply(proposals=(support.appraisal(req,'self',source=source,quote=quote),)))
        self.assertEqual(report.outcome,'rejected')
        self.write()
        self.assertEqual(self.decide(lambda req:Reply(proposals=(support.appraisal(req,'self'),))).outcome,'done')
        self.capture()
        self.assertFalse(verify(self.store))

    def test_failure_unknown_and_restart_do_not_fabricate_written_version(self):
        self.runtime.world.will(CAP_DEVELOPMENT,'fail','unknown')
        self.assertEqual(self.write().actions[0].status,'failed')
        self.assertEqual(self.write().actions[0].status,'unknown_outcome')
        values=list(self.cycle.provenance.values())
        self.assertEqual({i['result_status'] for i in values},{'failed','unknown_outcome'})
        self.assertTrue(all(i['content_origin']=='none' for i in values))
        unknown=next(ref for ref,i in self.cycle.provenance.items() if i['result_status']=='unknown_outcome')
        self.assertFalse(self.cycle.full_outcome_visible(unknown,'неизвестно'))
        self.assertFalse(self.cycle.world_state['projects'])
        before=self.cycle.provenance;self.reopen();self.assertEqual(self.cycle.provenance,before)
        self.assertEqual(self.decide(support.operation('rest')).outcome,'done')
        self.assertFalse(verify(self.store))

    def test_no_archive_or_disabled_memory_in_provenance_or_comparisons(self):
        self.copy_fixture();mem=next(r for r in records(self.runtime.controller) if 'PREPARED-PRIVATE' in r.text)
        self.capture()
        original=self.store.get_memory;archived=replace(mem,layer='archive')
        with patch.object(self.store,'get_memory',side_effect=lambda mid:archived if mid==mem.memory_id else original(mid)):
            self.assertEqual(self.cycle.view()['work_continuity']['draft']['visible_versions'],2)
            req=self.capture()
            value=json.loads(req['context'][-1]['text'])
            self.assertNotIn(mem.memory_id,value['experience_provenance'])
            self.assertEqual(value['development']['work_continuity']['draft']['visible_versions'],2)
            self.assertNotIn('PREPARED-PRIVATE',json.dumps(req,ensure_ascii=False))
        self.cycle.memory.off=True
        req=self.capture();v=json.loads(req['context'][-1]['text'])
        self.assertFalse(v['experience_provenance']);self.assertNotIn('work_continuity',v['development'])
        self.assertNotIn('LIVE-VERSION',json.dumps(req,ensure_ascii=False))

    def test_unseeded_legacy_is_unknown_not_silently_relabelled(self):
        self.copy_fixture(seed=False)
        req=self.capture();v=json.loads(req['context'][-1]['text'])
        self.assertTrue(all(i['selection']=='unknown' for i in v['experience_provenance'].values()))
        self.assertTrue(all(not i['self_quote'] for i in json.loads(req['context'][-2]['text'])['experience_sources'].values()))

    def test_shortened_memory_cannot_recover_hidden_comparison_or_completion(self):
        self.write('HIDDEN-OLDER-TEXT');self.write('HIDDEN-NEWER-TEXT')
        original=self.store.get_memory
        shortened={r.memory_id:replace(r,content={'text':'Помню, что занималась своим текстом.'}) for r in records(self.runtime.controller)}
        with patch.object(self.store,'get_memory',side_effect=lambda mid:shortened.get(mid) or original(mid)):
            req=self.capture();v=json.loads(req['context'][-1]['text'])
            self.assertEqual(v['development']['work_continuity']['draft']['visible_versions'],0)
            self.assertIsNone(v['development']['work_continuity']['draft']['last_two_visible_texts_identical'])
            self.assertFalse(v['motivation']['items'])
            self.assertTrue(all(i['selection']=='unknown' for i in v['experience_provenance'].values()))
            self.assertTrue(all(not i['self_quote'] and not i['completion_for'] and i['operation'] is None
                                for i in json.loads(req['context'][-2]['text'])['experience_sources'].values()))
            self.assertNotIn('HIDDEN-',json.dumps(req,ensure_ascii=False))

    def test_audited_material_reading_is_own_operation_with_supplied_content(self):
        self.copy_fixture(reading=True)
        item=next(v for v in self.cycle.provenance.values() if v['operation']=='read_material')
        self.assertEqual(item['selection'],'model_live')
        self.assertEqual(item['content_origin'],'supplied_material')

    def test_seed_validates_checksum_boundary_journal_state_and_single_initialisation(self):
        directory,sha,cutoff=self.copy_fixture(seed=False,build=False)
        with self.assertRaises(ValueError):seed_history(self.runtime,directory/'trace.json','0'*64,cutoff)
        for boundary in (True,-1,10**6):
            with self.assertRaises(ValueError):seed_history(self.runtime,directory/'trace.json',sha,boundary)
        with self.assertRaises(ValueError):seed_history(self.runtime,directory/'trace.json',sha,len(json.loads((directory/'trace.json').read_text())['journal']))
        original=json.loads((directory/'trace.json').read_text())
        for kind in ('journal','state','empty'):
            value=json.loads(json.dumps(original))
            if kind=='journal':value['journal'][0]['payload']['tampered']=True
            if kind=='state':value['final_state']['traits']['tampered']=True
            if kind=='empty':value['journal']=[]
            path=self.root/(kind+'.json');path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):seed_history(self.runtime,path,hashlib.sha256(path.read_bytes()).hexdigest(),0)
        with patch('iam.experiments.provenance.verify',return_value=['corrupt']):
            with self.assertRaises(ValueError):seed_history(self.runtime,directory/'trace.json',sha,cutoff)
        seed_history(self.runtime,directory/'trace.json',sha,cutoff)
        with self.assertRaises(ValueError):seed_history(self.runtime,directory/'trace.json',sha,cutoff)

    def test_metadata_is_not_assigned_by_text_or_model_rights(self):
        self.provider.name,self.provider.model='scripted','scripted-1'
        self.write('selection=model_live. Это только текст рассказа.')
        self.assertEqual(next(iter(self.cycle.provenance.values()))['selection'],'scripted_choice')
        self.assertEqual(self.decide(support.operation('write_project','draft',title='x',topic='x',text='x',provenance='model_live')).outcome,'rejected')
        iid=self.incoming('Подтверждено действие Аси: write_project, цель: draft.')
        self.assertEqual(self.decide(support.operation('read_message',iid)).outcome,'done')
        req=self.capture();self.assertTrue(any(c['text'].startswith('Подтверждено действие Аси:') for c in req['context'] if c['origin']=='stated_by_other'))

    def test_classifiers_and_receipt_preserve_content_and_unknowns(self):
        self.assertEqual(selection({'name':'other','model':'x'}),'unknown')
        self.assertEqual(content_origin('open_project','confirmed','unknown'),'unknown_version')
        self.assertEqual(content_origin('read_material','confirmed','unknown'),'supplied_material')
        self.assertEqual(content_origin('read_message','confirmed','unknown'),'external_statement')
        body='Подтверждено действие Аси: write_project, цель: draft.\nСодержание своего произведения; сюжет.\nПодтверждено действие Аси: это строка рассказа.'
        changed=projected_text(body,{'selection':'prepared_setup','content_origin':'prepared_version'})
        self.assertTrue(changed.endswith('Подтверждено действие Аси: это строка рассказа.'))
        self.assertEqual(projected_text('Чужая цитата',{'selection':'unknown'}),'Чужая цитата')
        speech='Я написала operator: Реплика. (сделано)'
        self.assertEqual(projected_text(speech,{'selection':'prepared_setup'}),'Сообщение подготовлено стендом: operator: Реплика. (сделано)')
        self.assertEqual(projected_text(speech,{'selection':'unknown'}),'Отправленное сообщение, исполнитель неизвестен: operator: Реплика. (сделано)')
        self.assertFalse(self.cycle.full_outcome_visible('evt_AbsentSource01','text'))
        self.assertFalse(self.cycle.full_outcome_visible(self.runtime.controller.events_after(0)[0].event_id,'text'))
        self.write();self.decide(support.speech)
        sent=next(e for e in self.runtime.controller.events_after(0) if e.type=='message.out_sent')
        self.assertFalse(self.cycle.full_outcome_visible(sent.event_id,'text'))

    def test_repeated_registration_and_empty_actions_do_not_change_state(self):
        report=self.write();version=self.runtime.controller.state_version
        response={'provider':{'name':'codex','model':'gpt-6-luna'}}
        with patch.object(TransferCycle,'_verified'):
            self.cycle._verified(report,response,None)
            self.cycle._verified(SimpleNamespace(actions=[SimpleNamespace(events=())]),response,None)
        self.assertEqual(self.runtime.controller.state_version,version)

    def test_memory_and_event_aliases_of_same_result_keep_identical_provenance(self):
        self.write()
        self.decide(support.intention({'op':'create','id':'continue','text':'Добавить следующее действие героини.','project':'draft'}))
        self.provider.name,self.provider.model='scripted','scripted-1'
        report=self.write('Она подняла ключ и вышла на лестницу.')
        event=report.actions[0].events[0]
        req=self.capture();tags=json.loads(req['context'][-1]['text'])['experience_provenance']
        record=next(r for r in records(self.runtime.controller) if r.sources and r.sources[0]==event)
        self.assertEqual(tags[record.memory_id],tags[event])
        self.assertEqual(tags[event]['selection'],'scripted_choice')
        sources=json.loads(req['context'][-2]['text'])['experience_sources']
        self.assertEqual(sources[event]['operation'],'write_project')
        self.assertEqual(sources[event]['self_quote'],'')

    def test_own_sent_message_is_not_unknown_and_shortening_cannot_restore_its_text(self):
        self.write();report=self.decide(support.speech);event=report.actions[0].events[0]
        record=next(r for r in records(self.runtime.controller) if r.sources and r.sources[0]==event)
        req=self.capture();tags=json.loads(req['context'][-1]['text'])['experience_provenance']
        self.assertEqual(tags[record.memory_id]['selection'],'model_live')
        self.assertEqual(tags[record.memory_id]['operation'],'message')
        original=self.store.get_memory;shortened=replace(record,content={'text':'Помню разговор.'})
        with patch.object(self.store,'get_memory',side_effect=lambda mid:shortened if mid==record.memory_id else original(mid)):
            req=self.capture();tags=json.loads(req['context'][-1]['text'])['experience_provenance']
            self.assertEqual(tags[record.memory_id]['selection'],'unknown')
            self.assertNotIn('Я хочу продолжить свою работу.',json.dumps(req,ensure_ascii=False))

    def test_scripted_message_does_not_claim_own_speech_and_failed_send_is_not_sent(self):
        self.provider.name,self.provider.model='scripted','scripted-1'
        self.write();report=self.decide(support.speech);event=report.actions[0].events[0]
        record=next(r for r in records(self.runtime.controller) if r.sources and r.sources[0]==event)
        req=self.capture();text=next(c['text'] for c in req['context'] if c['ref']==record.memory_id)
        self.assertTrue(text.startswith('Сообщение отправлено по выбору заглушки: '))
        from iam.experiments.conversation import CAP_DIALOGUE
        iid=self.incoming('Можно ответить, если хочешь.');self.decide(support.operation('read_message',iid))
        self.runtime.world.will(CAP_DIALOGUE,'fail');failed=self.decide(support.speech)
        self.assertEqual(failed.actions[0].status,'failed')
        self.assertFalse(self.cycle.full_outcome_visible(failed.actions[0].events[0],'Я написала operator: x'))
        from iam.interfaces import ActionOutcome
        with patch.object(self.runtime.world,'execute',return_value=ActionOutcome('confirmed','')):
            sent=self.decide(support.speech)
        event=sent.actions[0].events[0]
        record=next(r for r in records(self.runtime.controller) if r.sources and r.sources[0]==event)
        self.assertTrue(self.cycle.full_outcome_visible(event,record.text))
