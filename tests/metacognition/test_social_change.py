import copy
import json
from dataclasses import replace
from unittest.mock import patch

from iam.cycle.runner import input_id_for
from iam.domain import new_id
from iam.selfmodel.social import candidates, pairs, roots
from iam.social import SocialMemory
from iam.state import EventSpec, verify
from iam.stubs import Reply, StubCreatorBridge, attend, rest
from iam.world import PersistentWorld
from iam.world.cycle import LivingCycle
from metacognition.test_reflection import reflect
from state.helpers import set_
from world.helpers import LivingCase


class SocialChangeTest(LivingCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.assertTrue(self.c.apply(self.prop(self.c, set_('traits', 'traits',
            [{'name': 'openness', 'value': 0.5, 'source': 'initial'}]))).applied)
        self.number = 0

    def enable(self):
        self.m = SocialMemory(self.c)
        self.world = PersistentWorld(self.c)
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())

    def episode(self, text=None, interpreted=True, appraisal=False):
        self.number += 1
        iid = input_id_for('telegram', str(self.number))
        arrival = self.c.record_event(EventSpec('input.arrived', 'rx:telegram:' + str(self.number),
            {'input_id': iid, 'sender': '123', 'channel': 'telegram',
             'text': text or f'Разговор {self.number}: я изменил взгляд после собственного опыта.'})).event
        self.p.queue(Reply((attend('dialogue', iid),)))
        self.assertEqual(self.cycle.run(arrival).outcome, 'done')
        event = self.c.find_event(key='rd:' + iid)
        row = next(row for row in self.m.records() if event.event_id in row.sources)
        if not interpreted:
            return row, None
        kind = 'emotional_associative' if appraisal else 'semantic'
        spec = {'appraisal': {'valence': 0.3, 'arousal': 0.4, 'relevance': 0.6, 'regulation': 0.5}} if appraisal else {
            'social': {'person': self.m.contact(arrival), 'closeness': 0.5}}
        proposal = {'proposal_id': new_id('prop'), 'kind': 'memory_write', 'rationale': 'Осмыслить разговор',
            'cites': [row.memory_id], 'body': {'draft': {'kind': kind, 'origin': 'own_hypothesis',
                'content': {'text': f'Моя собственная оценка разговора {self.number}', **spec}}}}
        before = {r.memory_id for r in self.m.records()}
        self.p.queue(Reply((proposal, rest())))
        self.assertEqual(self.cycle.run(event).outcome, 'done')
        note = next(r for r in self.m.records() if r.memory_id not in before)
        return row, note

    def experiences(self, texts=None, **kwargs):
        result = []
        for i in range(3):
            result.append(self.episode(texts[i] if texts else None, **kwargs))
            self.time.advance(35_000)
        return result

    def change(self, experiences, **extra):
        refs = [row.memory_id for pair in experiences for row in pair if row is not None]
        body = {'part': 'now', 'level': 'stable_change', 'basis_kind': 'social_experience',
                'text': 'Считаю, что общение немного изменило мою открытость.', 'trait': 'openness', 'delta': 0.03}
        self.run_reply(reflect(), replies=(Reply((reflect({**body, **extra}, refs),)),))

    def test_real_read_interpretation_opt_in_atomic_change_and_restart_no_reuse(self):
        experiences = self.experiences(appraisal=True)
        self.assertEqual(self.c.state['traits']['traits'][0]['value'], 0.5)
        self.run_reply(rest())
        self.assertTrue(any('эпизодов общения с твоими' in i['text'] for i in self.p.calls[-1]['context']))
        self.change(experiences)
        self.assertEqual(self.c.state['traits']['traits'][0]['value'], 0.53)
        entry, = self.c.state['selfmodel']['reflections'].values()
        self.assertEqual(entry['basis_kind'], 'social_experience')
        self.assertEqual(len(entry['bases']), 3)
        self.assertEqual(set(entry['refs']), {r.memory_id for pair in experiences for r in pair})
        ref_request = self.p.calls[-1]
        self.assertTrue(set(entry['refs']) <= {i['ref'] for i in ref_request['context']})
        self.assertEqual(self.cycle.reflection.model.view()['parts']['now']['traits'][0]['value'], 0.53)
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        self.enable()
        self.c.resume_session()
        self.change(experiences)
        self.assertEqual(len(self.c.state['selfmodel']['reflections']), 1)
        self.assertEqual(self.c.state['traits']['traits'][0]['value'], 0.53)
        self.assertEqual(verify(self.c.store), [])

    def test_duplicate_words_two_episodes_or_missing_interpretation_do_not_change(self):
        experiences = self.experiences(['Одни слова', '  ОДНИ   СЛОВА ', 'Другие слова'])
        self.change(experiences)
        self.assertFalse(self.c.state['selfmodel'].get('reflections'))
        self.change(experiences[:2])
        self.change([(row, None) for row, _ in experiences])
        self.assertEqual(self.c.state['traits']['traits'][0]['value'], 0.5)
        raw = self.episode('Без собственной оценки', interpreted=False)
        self.assertNotIn(raw[0].memory_id, str(pairs(self.m, self.cycle.reflection.model.records())))

    def test_short_span_wrong_kind_delta_or_mixed_action_path_rejected(self):
        experiences = [self.episode() for _ in range(3)]
        self.change(experiences)
        self.assertEqual(self.c.state['traits']['traits'][0]['value'], 0.5)
        self.time.advance(65_000)
        experiences[-1] = self.episode()
        for fields in ({'basis_kind': 'unknown'}, {'delta': 0.06}, {'delta': True}, {'trait': 'missing'}):
            self.change(experiences, **fields)
        self.assertFalse(self.c.state['selfmodel'].get('reflections'))
        body = {'part': 'now', 'level': 'stable_change', 'text': 'Legacy needs actions', 'trait': 'openness', 'delta': 0.03}
        self.run_reply(reflect(), replies=(Reply((reflect(body, [r.memory_id for pair in experiences for r in pair]),)),))
        self.assertFalse(self.c.state['selfmodel'].get('reflections'))

    def test_forgetting_revision_off_and_unread_never_supply_social_bases(self):
        experiences = self.experiences()
        model = self.cycle.reflection.model
        self.assertEqual(len(candidates(model, self.c.state, 3, 60_000)), 6)
        self.assertTrue(self.m.fade(experiences[0][0].memory_id).applied)
        self.assertEqual(candidates(model, self.c.state, 3, 60_000), [])
        self.change(experiences)
        self.assertFalse(self.c.state['selfmodel'].get('reflections'))
        self.assertTrue(self.m.revise(experiences[1][1].memory_id, 'Новая интерпретация',
            sources=experiences[1][1].sources, reason='Уточнение').applied)
        self.assertEqual(len(pairs(self.m, model.records())), 1)
        self.m.off = True
        self.assertEqual(pairs(self.m, model.records()), {})

    def test_roots_require_both_members_and_valid_primary_metadata(self):
        source, note = self.episode()
        rows = {source.memory_id: source, note.memory_id: note}
        self.assertEqual(roots(self.m, rows, [source.memory_id]), {})
        self.assertEqual(len(roots(self.m, rows, list(rows))), 1)
        for source_change in ({'version': 2}, {'kind': 'semantic'}, {'origin': 'own_hypothesis'},
                              {'sources': ()}, {'content': {**source.content, 'experience': 'prepared'}},
                              {'content': {**source.content, 'text': '  '}}):
            altered = {**rows, source.memory_id: replace(source, **source_change)}
            self.assertEqual(pairs(self.m, altered), {})
        for note_change in ({'version': 2}, {'origin': 'model_explanation'}, {'sources': ()}):
            self.assertEqual(pairs(self.m, {**rows, note.memory_id: replace(note, **note_change)}), {})
        self.assertEqual(pairs(self.m, {note.memory_id: note}), {})
        with patch.object(self.c, 'find_event', return_value=None):
            self.assertEqual(pairs(self.m, rows), {})

    def test_candidate_budget_safety_traits_and_no_auto_change(self):
        self.experiences()
        model = self.cycle.reflection.model
        selected = candidates(model, self.c.state, 3, 60_000)
        self.assertLessEqual(sum(len(i['text']) for i in selected), 6000)
        state = copy.deepcopy(self.c.state)
        state['traits']['traits'] = [None, {'name': 'other', 'value': 'high'}]
        self.assertEqual(candidates(model, state, 3, 60_000), [])
        with patch.object(self.m, '_window', return_value=None):
            self.assertEqual(candidates(model, self.c.state, 3, 60_000), [])
        self.assertEqual(self.c.state['traits']['traits'][0]['value'], 0.5)

    def test_punctuation_repeats_and_interpretation_aliases_are_one_experience(self):
        experiences = self.experiences(['Одни слова.', '«ОДНИ слова!»', 'Одни слова?'])
        self.change(experiences)
        self.assertFalse(self.c.state['selfmodel'].get('reflections'))
        source, note = experiences[0]
        later = replace(note, memory_id=new_id('mem'), created_life_ms=note.created_life_ms + 1)
        rows = {row.memory_id: row for row in (source, note, later)}
        self.assertEqual(len(roots(self.m, rows, [source.memory_id, note.memory_id])), 1)
        self.assertEqual(len(roots(self.m, rows, list(rows))), 1)

    def test_large_history_packs_only_three_selected_pairs(self):
        self.experiences()
        self.experiences()
        with patch.object(self.m, '_window', wraps=self.m._window) as window:
            selected = candidates(self.cycle.reflection.model, self.c.state, 3, 60_000)
        self.assertEqual((len(selected), window.call_count), (6, 6))
