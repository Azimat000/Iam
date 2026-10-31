"""Контракты S19 на новых базах: настоящее ядро, управляемые часы, без живой модели."""
from dataclasses import replace
import json
import random

from iam.domain import Link, Op, new_id
from iam.memory import PersistentMemory
from iam.memory.lifecycle import WEEK_MS
from iam.state import EventSpec, verify
from state.helpers import StateCase


class LifecycleTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)
        self.m = PersistentMemory(self.c)

    def put(self, text='мысль', **changes):
        args = dict(kind='semantic', origin='own_hypothesis', text=text, sources=(self.root(self.c),))
        args.update(changes)
        r = self.m.prepare(**args)
        result = self.m.write(r)
        self.assertTrue(result.applied, result)
        return self.c.store.get_memory(r.memory_id)

    def test_generalization_revision_and_current_context(self):
        one = self.put('вечером читала главу', kind='episodic')
        two = self.put('вечером дочитала рассказ', kind='episodic')
        summary, result = self.m.generalize([one.memory_id, two.memory_id, one.memory_id], 'вечером нравится читать')
        self.assertTrue(result.applied, result)
        current = self.c.store.get_memory(summary.memory_id)
        self.assertEqual(current.sources, (one.memory_id, two.memory_id))
        self.assertEqual((current.layer, current.origin), ('interpretation', 'own_hypothesis'))
        old = self.put('дата 3 марта', origin='stated_by_other', claim_kind='fact_claim', independence_group='operator')
        cached = self.m.retrieve('дата')
        self.c.record_event(EventSpec('input.arrived', 'rx:after:prepare', {}))
        result = self.m.revise(old.memory_id, 'дата 5 марта', sources=old.sources, reason='исправление даты')
        self.assertTrue(result.applied, result)
        r = self.c.store.get_memory(old.memory_id)
        self.assertEqual(r.version, 2)
        self.assertEqual(r.history[-1]['previous_text'], old.text)
        self.assertEqual(r.history[-1]['at_seq'], result.events[0].seq)
        self.assertEqual(result.events[0].type, 'memory.revised')
        self.assertEqual(result.events[0].payload['supersedes']['version'], 1)
        context = json.dumps(self.m.context(None, {'ref':'x', 'text':'вопрос'}, cached), ensure_ascii=False)
        self.assertIn('5 марта', context)
        self.assertNotIn('3 марта', context)
        self.assertEqual(self.m.retrieve('3 марта'), [])
        self.assertEqual(verify(self.c.store), [])
        with self.assertRaises(ValueError): self.m.generalize([], 'нет')
        with self.assertRaises(ValueError): self.m.generalize([old.memory_id], 'не эпизод')
        with self.assertRaises(ValueError): self.m.revise(old.memory_id, '', sources=old.sources, reason='нет текста')
        with self.assertRaises(ValueError): self.m.revise(old.memory_id, 'x', sources=(), reason='нет источников')

    def test_repetition_property_and_unknown_independence(self):
        a = self.put('критика', origin='stated_by_other', claim_kind='fact_claim', independence_group='person:a')
        b = self.put('иная критика', origin='stated_by_other', claim_kind='fact_claim', independence_group='person:b')
        ids = [a.memory_id, b.memory_id]
        support = self.m.support(ids)
        self.assertEqual(support['independent_support'], 0)
        self.assertEqual(support['unknown_groups'], ['person:a', 'person:b'])
        self.assertTrue(self.m.confirm_independence(support['unknown_groups'], reason='проверены два разных первоисточника').applied)
        baseline = self.m.support(ids)
        self.assertEqual(baseline['independent_support'], 2)
        # Воспроизводимые случайные деревья повторов. Число записей и глубина не меняют опор.
        rng = random.Random(1919)
        for i in range(48):
            parent = rng.choice(ids)
            rec = self.put('пересказ ' + str(i), sources=(parent,), layer='interpretation')
            ids.append(rec.memory_id)
            self.assertEqual(self.m.support(rng.choices(ids, k=100))['independent_support'], 2)
            self.assertEqual(self.m.support(ids), baseline)
        own = self.put('моя гипотеза')
        repeat = self.put('опять моя гипотеза', sources=(own.memory_id,))
        self.assertEqual(self.m.support([own.memory_id, repeat.memory_id])['independent_support'], 0)
        self.assertEqual(len(self.m.support([own.memory_id, repeat.memory_id])['self_groups']), 1)
        statement = self.put('факт сообщения', origin='observation', claim_kind='statement_made',
                             independence_group='person:c', sources=(self.observed('input.read').event_id,))
        self.assertEqual(self.m.support([statement.memory_id])['independent_support'], 0)
        for groups in ([], ['person:a'], ['person:a', 'missing']):
            with self.assertRaises(ValueError): self.m.confirm_independence(groups, reason='ошибка')
        for actor in ('model', 'core'):
            res = self.c.apply(self.prop(self.c, Op('world_time', 'world.memory_independence', 'set', ['forged'], True), actor=actor))
            self.assertFalse(res.applied)
        self.assertEqual(self.m.support(ids)['independent_support'], 2)

    def observed(self, type='activity.completed', **payload):
        key = ('rd:' + new_id('inp')) if type == 'input.read' else 'act:' + new_id('act') + ':done'
        result = self.c.apply(self.prop(self.c, Op('world_time', 'world.last_observation', 'set', 'ok', True),
                            actor='core' if type == 'input.read' else 'world', carrier=EventSpec(type, key, payload)))
        self.assertTrue(result.applied, result)
        return result.events[0]

    def test_links_dependencies_versions_and_hidden_paths(self):
        original = self.put('героиня Лидия', kind='episodic')
        dependent = self.put('её зовут Лидия', sources=(original.memory_id,))
        summary, result = self.m.generalize([original.memory_id], 'Героиня Лидия знакома')
        self.assertTrue(result.applied)
        self.assertTrue(self.m.link(dependent.memory_id, original.memory_id).applied)
        before = self.m.retrieve('Лидия')
        for group, path in [('intentions','goal'), ('preferences','motivation'), ('selfmodel','assessment')]:
            self.assertTrue(self.c.apply(self.prop(self.c, Op(group, path, 'set',
                         {'text':'Лидия', 'sources':[original.memory_id]}, True))).applied)
        self.assertTrue(self.m.fade(original.memory_id).applied)
        self.assertEqual(self.m.records(), [])
        self.assertEqual(self.m.retrieve('Лидия'), [])
        self.assertIsNone(self.m._source(original.memory_id))
        context = self.m.context(None, {'ref':'x','text':'Героиня Лидия знакома'}, before)
        self.assertNotIn('Лидия', json.dumps(context, ensure_ascii=False))
        self.assertEqual(self.m.sensory, context)
        state = self.m.decision_state()
        self.assertEqual(state['intentions'], {})
        self.assertEqual(state['preferences'], {})
        self.assertEqual(state['selfmodel'], {})
        for mid in (original.memory_id, dependent.memory_id, summary.memory_id):
            with self.assertRaises(ValueError): self.m.fade(mid)
        # Источник в архиве остался точным; прежний слепок не восстановил его в память.
        event, archived = self.m._archive_version(original.memory_id, 1)
        self.assertEqual(archived.text, 'героиня Лидия')
        self.assertEqual(self.c.store.get_memory(original.memory_id).availability, 0)
        self.assertEqual(verify(self.c.store), [])

    def test_revision_invalidates_old_summaries_and_event_sources(self):
        event = self.observed('input.read')
        old = self.put('старый адрес', kind='episodic', sources=(event.event_id,))
        derived = self.put('кеш старого адреса', sources=(old.memory_id,))
        new_event = self.observed('input.read')
        self.assertTrue(self.m.revise(old.memory_id, 'новый адрес', sources=(new_event.event_id,), reason='новые данные').applied)
        self.assertNotIn(derived.memory_id, {r.memory_id for r in self.m.records()})
        self.assertIsNone(self.m._source(event.event_id))
        self.assertFalse(self.m.write(self.m.prepare(kind='episodic', origin='observation', text='старый адрес', sources=(event.event_id,))).applied)
        self.assertTrue(self.c.apply(self.prop(self.c, Op('intentions', 'goal', 'set', {'text':'старый адрес'}, True))).applied)
        self.assertEqual(self.m.decision_state()['intentions'], {})

    def test_life_time_fading_pause_and_off(self):
        old = self.put('эпизод', kind='episodic')
        self.put('обобщение', sources=(old.memory_id,))
        self.time.advance(WEEK_MS - 1)
        self.assertEqual(self.m.fade_due(), [])
        self.c.pause_session('operator')
        self.time.advance(WEEK_MS)
        self.assertEqual(self.m.fade_due(), [])
        self.c.resume_session()
        self.m.off = True
        self.time.advance(1)
        self.assertEqual(self.m.fade_due(), [])
        self.m.off = False
        self.assertEqual(self.m.retrieve(''), [])
        self.assertEqual(self.c.store.get_memory(old.memory_id).detail, 0)
        self.assertEqual(self.types(self.c).count('memory.faded'), 1)
        self.assertEqual(verify(self.c.store), [])

    def test_fade_bounds_and_invalid_writes(self):
        r = self.put()
        for kw in ({'availability':1.1}, {'availability':True}, {'detail':float('nan')}, {'detail':-1}):
            with self.assertRaises(ValueError): self.m.fade(r.memory_id, **kw)
        self.assertTrue(self.m.fade(r.memory_id, availability=.8, detail=1).applied)
        with self.assertRaises(ValueError): self.m.fade(r.memory_id, availability=.9, detail=1)
        with self.assertRaises(ValueError): self.m.link(r.memory_id, r.memory_id)
        self.assertFalse(self.m.link(r.memory_id, 'mem_Missing001').applied)
        current = self.c.store.get_memory(r.memory_id)
        for edited in (replace(current, version=3, origin='imagination'),
                       replace(current, version=4), replace(current, version=3, history=()),
                       replace(current, version=3, sources=(current.memory_id,)),
                       replace(current, version=3, links=(Link('same_source', current.memory_id),))):
            self.assertFalse(self.c.apply(self.prop(self.c, Op('memory', r.memory_id, 'set', edited.to_dict(), True))).applied)
        with self.assertRaises(ValueError): self.m._move_memory([('set', r)], 'memory.faded', '')
        self.m.off = True
        with self.assertRaises(ValueError): self.m.fade(r.memory_id)
        with self.assertRaises(ValueError): self.m.restore_from_archive(r.memory_id, 1, reason='off')

    def test_restore_is_new_atomic_operator_record_without_laundering(self):
        event = self.observed('input.read')
        r = self.put('архивная деталь <script>внешнее</script>', origin='stated_by_other',
                     claim_kind='fact_claim', independence_group='human', sources=(event.event_id,))
        self.assertTrue(self.m.fade(r.memory_id).applied)
        restored, result = self.m.restore_from_archive(r.memory_id, 1, reason='осознанно вернуть деталь')
        self.assertTrue(result.applied, result)
        self.assertNotEqual(restored.memory_id, r.memory_id)
        self.assertEqual([e.type for e in result.events], ['operator.intervention', 'memory.recorded'])
        self.assertEqual(result.events[1].causes, (result.events[0].event_id,))
        self.assertEqual(result.events[0].source.kind, 'operator_panel')
        self.assertEqual(result.events[0].payload['kind'], 'restore_from_archive')
        current = self.c.store.get_memory(restored.memory_id)
        self.assertEqual((current.origin, current.claim_kind, current.independence_group), (r.origin, r.claim_kind, r.independence_group))
        self.assertEqual(current.text, r.text)
        self.assertEqual(current.availability, .6)
        self.assertEqual(len(self.m.retrieve('деталь')), 1)
        self.assertEqual(verify(self.c.store), [])
        # Без вмешательства, с подменой источника/текста и без архивной версии восстановить нельзя.
        for forged in (replace(current, memory_id=new_id('mem')), replace(current, memory_id=new_id('mem'), content={'text':'подмена','archive_ref':{}})):
            self.assertFalse(self.m.write(forged).applied)
        _, bad = self.m.restore_from_archive(r.memory_id, 1, reason='')
        self.assertFalse(bad.applied)
        with self.assertRaises(ValueError): self.m.restore_from_archive(r.memory_id, 777, reason='нет версии')
        self.assertTrue(self.m.fade(current.memory_id).applied)
        self.assertEqual(self.m.retrieve('деталь'), [])
        self.c = self.reopen(self.c)
        self.m = PersistentMemory(self.c)
        self.assertEqual(self.m.retrieve('деталь'), [])
        self.assertEqual(verify(self.c.store), [])

    def test_restore_does_not_make_unknown_or_failed_success(self):
        # Самостоятельный Cycle-набор отдельно в test_cycle; здесь проверяется архив исхода.
        for status, event_type in [('failed','action.failed'), ('unknown','action.unknown_outcome')]:
            key = 'act:' + new_id('act') + (':fail' if status == 'failed' else ':unk')
            action_id = key.split(':')[1]
            event = self.c.apply(self.prop(self.c, Op('world_time','world.result','set',status,True), actor='core' if status == 'unknown' else 'world',
                        carrier=EventSpec(event_type, key, {'action_id':action_id}))).events[0]
            r = self.put(status, kind='episodic', origin='observation', sources=(event.event_id,),
                         content={'experience':'live','outcome':status,'action_id':action_id})
            self.assertTrue(self.m.fade(r.memory_id).applied)
            restored, result = self.m.restore_from_archive(r.memory_id, 1, reason='вернуть факт исхода')
            self.assertTrue(result.applied, result)
            self.assertEqual(restored.content['outcome'], status)
            bad = replace(restored, memory_id=new_id('mem'), content={**restored.content,'outcome':'confirmed'})
            self.assertFalse(self.c.restore_memory(bad, reason='подмена').applied)
        self.assertEqual(verify(self.c.store), [])

    def test_same_source_links_follow_source_version(self):
        source = self.put('старый адрес', kind='episodic')
        retelling = self.put('кеш старого адреса')
        self.assertTrue(self.m.link(retelling.memory_id, source.memory_id).applied)
        self.assertTrue(self.m.revise(source.memory_id, 'новый адрес', sources=source.sources, reason='исправление').applied)
        self.assertEqual(self.m.retrieve('старого'), [])
        self.assertEqual(len(self.m.context(None, {'ref':'x','text':'вопрос'}, [retelling])), 1)

    def test_context_budget_and_restore_validation_edges(self):
        from iam.state.controller import Check
        from unittest.mock import patch
        r = self.put('длинная проверочная запись')
        tiny = PersistentMemory(self.c, text_limit=1)
        self.assertEqual(tiny.context(None, {'ref':'x','text':'новое'}, [r]), [{'ref':'x','text':'н'}])
        forged = replace(r, memory_id=new_id('mem'), restored_by_operator=True, content={**r.content, 'archive_ref': {'memory_id':'mem_Missing001','version':1}})
        self.assertFalse(self.c.restore_memory(forged, reason='несуществующая версия').applied)
        with patch.object(self.m, '_archive_version', return_value=(None, replace(r, origin='simulation_event'))):
            with self.assertRaises(ValueError): self.m.restore_from_archive(r.memory_id, 1, reason='симуляция')
        old = self.c.store.get_memory(r.memory_id)
        self.assertTrue(self.m.fade(r.memory_id, availability=.5, detail=1).applied)
        current = self.c.store.get_memory(r.memory_id)
        for availability, detail in [(None,None), (.8,1), (.5,None), (.5,2)]:
            bad = replace(current, version=current.version+1, availability=availability, detail=detail)
            self.assertFalse(self.c.apply(self.prop(self.c, Op('memory',r.memory_id,'set',bad.to_dict(),True),
                            carrier=EventSpec('memory.faded','fad:'+new_id('trn')))).applied)

    def test_legacy_unpinned_source_cannot_keep_old_details(self):
        source = self.put('прежний номер')
        cached = self.m.prepare(kind='semantic', origin='own_hypothesis', text='кеш: прежний номер', sources=(source.memory_id,))
        content = {k:v for k,v in cached.content.items() if k != 'source_versions'}
        self.assertTrue(self.m.write(replace(cached, content=content)).applied)
        self.assertTrue(self.m.revise(source.memory_id, 'новый номер', sources=source.sources, reason='исправление').applied)
        self.assertEqual(self.m.retrieve('прежний'), [])
        self.assertFalse(self.c.restore_memory(replace(cached, memory_id=new_id('mem'), restored_by_operator=True,
                                  content={'text':'ошибка','archive_ref':'строка'}), reason='проверка').applied)

    def test_restore_retelling_does_not_add_independent_group(self):
        a = self.put('сказал a', origin='stated_by_other', claim_kind='fact_claim', independence_group='a')
        b = self.put('b пересказал a', origin='stated_by_other', claim_kind='fact_claim', independence_group='b', sources=(a.memory_id,))
        other = self.put('b наблюдал другое', origin='stated_by_other', claim_kind='fact_claim', independence_group='b')
        self.assertTrue(self.m.confirm_independence(['a','b'], reason='два проверенных источника').applied)
        self.assertEqual(self.m.support([a.memory_id,b.memory_id])['independent_support'], 1)
        self.assertTrue(self.m.fade(b.memory_id).applied)
        restored, result = self.m.restore_from_archive(b.memory_id, 1, reason='вернуть тот же пересказ')
        self.assertTrue(result.applied, result)
        self.assertEqual(self.m.support([a.memory_id,restored.memory_id])['independent_support'], 1)
        # Повторное восстановление сохраняет те же корни, не рождает новую группу.
        again, result = self.m.restore_from_archive(restored.memory_id, 1, reason='повторное восстановление')
        self.assertTrue(result.applied)
        self.assertEqual(self.m.support([again.memory_id])['confirmed_groups'], ['a'])
        current = self.c.store.get_memory(again.memory_id)
        bad = self.m._version(current, 'подмена', content={**current.content,'restored_support':{'groups':['b'],'self_groups':[]}})
        self.assertFalse(self.c.apply(self.prop(self.c, Op('memory',current.memory_id,'set',bad.to_dict(),True))).applied)

    def test_revision_preserves_existing_link_version(self):
        a = self.put('первая дата')
        self.assertTrue(self.m.revise(a.memory_id,'вторая дата',sources=a.sources,reason='уточнение').applied)
        b = self.put('пересказ второй даты')
        self.assertTrue(self.m.link(b.memory_id,a.memory_id).applied)
        self.assertTrue(self.m.revise(b.memory_id,'уточнённый пересказ',sources=b.sources,reason='формулировка').applied)
        self.assertIn(b.memory_id, {r.memory_id for r in self.m.records()})

    def test_restored_revision_uses_new_bases_instead_of_old_attestation(self):
        a = self.put('сведения a', origin='stated_by_other', claim_kind='fact_claim', independence_group='a')
        b = self.put('сведения b', origin='stated_by_other', claim_kind='fact_claim', independence_group='b')
        c = self.put('непроверенное c', origin='stated_by_other', claim_kind='fact_claim', independence_group='unknown-c')
        self.assertTrue(self.m.confirm_independence(['a','b'], reason='проверенные источники').applied)
        self.assertTrue(self.m.fade(a.memory_id).applied)
        r, result = self.m.restore_from_archive(a.memory_id, 1, reason='вернуть a')
        self.assertTrue(result.applied)
        self.assertEqual(self.m.support([r.memory_id])['independent_support'], 1)
        self.assertTrue(self.m.revise(r.memory_id, 'исправленное утверждение c', sources=(c.memory_id,), reason='смена основания').applied)
        self.assertEqual(self.m.support([r.memory_id])['independent_support'], 0)
        self.assertEqual(self.m.support([r.memory_id])['unknown_groups'], ['unknown-c'])
        r2, result = self.m.restore_from_archive(r.memory_id, 2, reason='вернуть пересмотренную версию')
        self.assertTrue(result.applied, result)
        self.assertEqual(self.m.support([r2.memory_id])['unknown_groups'], ['unknown-c'])
        self.assertEqual(self.m.support([r2.memory_id])['independent_support'], 0)

    def test_restore_diamond_retellings_share_one_root(self):
        a = self.put('один источник', origin='stated_by_other', claim_kind='fact_claim', independence_group='a')
        b = self.put('пересказ b', sources=(a.memory_id,))
        c = self.put('пересказ c', sources=(a.memory_id,))
        d = self.put('сводка b и c', sources=(b.memory_id,c.memory_id))
        r, result = self.m.restore_from_archive(d.memory_id, 1, reason='вернуть сводку')
        self.assertTrue(result.applied, result)
        self.assertEqual(self.m.support([r.memory_id])['unknown_groups'], ['a'])

    def test_attention_requires_available_versioned_bases(self):
        r = self.put('доступный опыт')
        for key, value in [('good',{'text':'занятие','sources':[r.memory_id]}),
                           ('event',{'text':'момент','sources':list(r.sources)}),
                           ('missing',{'text':'ошибка','sources':['mem_Missing001']}),
                           ('plain','нет оснований')]:
            self.assertTrue(self.c.apply(self.prop(self.c,Op('intentions',key,'set',value,True))).applied)
        self.assertEqual(set(self.m.decision_state()['intentions']), {'good'})
        self.assertTrue(self.m.revise(r.memory_id,'обновлённый опыт',sources=r.sources,reason='уточнение').applied)
        self.assertEqual(set(self.m.decision_state()['intentions']), set())
        self.assertTrue(self.c.apply(self.prop(self.c,Op('intentions','good','set',
                         {'text':'новое занятие','sources':[r.memory_id],'source_versions':{r.memory_id:2}},True))).applied)
        self.assertEqual(set(self.m.decision_state()['intentions']), {'good'})
