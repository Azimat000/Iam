"""Новая оценка выполняется, но не назначает целей и не обходит проверяющее ядро."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

from iam.domain import new_id
from iam.state import verify
from iam.stubs import Reply, ScriptedProvider, StubCreatorBridge, act, rest
from iam.world import WRITE
from iam.world.prospective import ProspectiveCycle
from world.helpers import LivingCase


class ProspectiveTest(LivingCase):
    def rebuild(self, cycle_type):
        from iam.world import PersistentWorld
        from iam.world.cycle import WorldMemory
        self.c = self.reopen(self.c)
        self.world, self.m = PersistentWorld(self.c), WorldMemory(self.c)
        self.cycle = cycle_type(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        if self.c.session_state == 'paused': self.c.resume_session()

    def enable(self):
        self.rebuild(ProspectiveCycle)

    def test_old_route_has_no_assessment_and_new_route_applies_goal_before_fresh_action(self):
        source = self.read()
        self.assertEqual([q['purpose'] for q in self.p.calls], ['choose_action'])
        self.enable()
        goal = self.goal(source)
        report = self.run_reply(goal, replies=(Reply((act(WRITE, {
            'project_id': 'poem', 'title': 'Работа', 'text': 'Свет у окна'}),)),))
        self.assertEqual(report.outcome, 'done')
        appraisal, choice = self.p.calls[-2:]
        self.assertEqual((appraisal['purpose'], choice['purpose']), ('appraise', 'choose_action'))
        self.assertEqual(appraisal['allowed_proposals'], ['memory_write', 'intention'])
        self.assertIn(WRITE, appraisal['allowed_actions'])
        self.assertLess(appraisal['state_version'], choice['state_version'])
        self.assertIn('my_goal', json.dumps(choice['context']))
        self.assertNotIn('intention', choice['allowed_proposals'])
        self.assertEqual(report.actions[0].status, 'confirmed')
        self.assertEqual(self.c.state['intentions']['my_goal']['criterion'], goal['body']['criterion'])
        self.assertEqual(verify(self.c.store), [])

    def test_empty_assessment_is_a_real_decline_and_does_not_create_goals_or_emotions(self):
        self.read(); self.enable()
        self.assertEqual(self.run_reply(replies=(Reply((rest(),)),)).outcome, 'done')
        self.assertFalse(self.c.state['intentions'])
        self.assertNotIn('emotion', self.c.state['affect'])
        self.assertIn('intention', self.p.calls[-1]['allowed_proposals'])
        self.assertTrue(any(event.payload.get('phase') == 'experience_appraisal_reserved'
                            for event in self.c.store.events()))

    def test_accepted_emotion_is_visible_to_next_action_in_same_cycle(self):
        source = self.read(); self.enable()
        interpretation = {'proposal_id': new_id('prop'), 'kind': 'memory_write',
                          'rationale': 'Подготовленная проверка оценки', 'cites': [source.memory_id],
                          'body': {'draft': {'kind': 'emotional_associative', 'origin': 'own_hypothesis',
                          'content': {'text': 'Заинтересовала перемена света', 'appraisal': {
                              'valence': .5, 'arousal': .3, 'relevance': .7, 'regulation': .2}}}}}
        self.assertEqual(self.run_reply(interpretation, replies=(Reply((rest(),)),)).outcome, 'done')
        self.assertIn('emotion', self.c.state['affect'])
        self.assertTrue(any('"emotion"' in item['text'] for item in self.p.calls[-1]['context']))

    def test_same_experience_and_same_trigger_are_not_reappraised_after_restart(self):
        self.read(); self.enable()
        trigger = self.due()
        self.p.queue(Reply(()), Reply((rest(),)))
        self.assertEqual(self.cycle.run(trigger).outcome, 'done')
        count = len(self.p.calls)
        self.enable()
        self.assertEqual(self.cycle.run(trigger).reason, 'already_done')
        self.assertEqual(len(self.p.calls), count)
        self.assertEqual(self.run_reply(rest()).outcome, 'done')
        self.assertEqual(len(self.p.calls), count + 1)
        self.assertEqual(self.p.calls[-1]['purpose'], 'choose_action')

    def test_off_and_prepared_or_hidden_basis_never_start_an_appraisal(self):
        self.read(); self.enable()
        self.m.off = True
        self.assertEqual(self.run_reply(rest()).outcome, 'done')
        self.assertEqual(self.p.calls[-1]['purpose'], 'choose_action')
        self.m.off = False; self.cycle.motivation.off = True
        self.assertEqual(self.run_reply(rest()).outcome, 'done')
        self.cycle.motivation.off = False
        with patch.object(self.m, 'retrieve', return_value=[]):
            self.assertEqual(self.run_reply(rest()).outcome, 'done')
        self.assertFalse(any(q['purpose'] == 'appraise' for q in self.p.calls))

    def test_prepared_hypothesis_or_nonvisible_experience_is_not_appraised_or_accepted_as_goal_basis(self):
        source = self.read(); self.enable()
        prepared = self.m.prepare(kind='episodic', origin='own_hypothesis', text='Подготовленная гипотеза',
                                  sources=(source.memory_id,), content={'experience': 'prepared'})
        self.assertTrue(self.m.write(prepared).applied)
        with patch.object(self.m, 'retrieve', return_value=[prepared]):
            self.assertEqual(self.run_reply(self.goal(prepared)).outcome, 'rejected')
        self.assertFalse(self.c.state['intentions'])
        self.assertEqual(self.p.calls[-1]['purpose'], 'choose_action')

        original = self.cycle._context
        def without_basis(*args):
            return [item for item in original(*args) if item['ref'] != source.memory_id]
        with patch.object(self.cycle, '_context', without_basis), patch.object(self.m, 'retrieve', return_value=[source]):
            self.assertEqual(self.run_reply(self.goal(source)).outcome, 'rejected')
        self.assertFalse(self.c.state['intentions'])
        self.assertEqual(self.p.calls[-1]['purpose'], 'choose_action')

    def interpretation(self, source, *, motivation=False):
        content = {'text': 'UNIQUE_APPRAISAL_REASONING'}
        if motivation:
            content['motivation'] = {'topic': 'poem', 'interest': .7, 'need': .2}
        return {'proposal_id': new_id('prop'), 'kind': 'memory_write', 'rationale': 'Подготовленная проверка',
                'cites': [source.memory_id], 'body': {'draft': {'kind': 'semantic',
                'origin': 'own_hypothesis', 'content': content}}}

    def test_generic_and_motivation_interpretations_reach_following_context_within_limits(self):
        for motivation in (False, True):
            from iam.world.cycle import LivingCycle
            self.rebuild(LivingCycle)
            source = self.write('Новый опыт ' + str(motivation), project='motivation_' + str(motivation))
            self.enable()
            before = {row.memory_id for row in self.m.records()}
            report = self.run_reply(self.interpretation(source, motivation=motivation), replies=(Reply((rest(),)),))
            self.assertEqual(report.outcome, 'done')
            created = {row.memory_id for row in self.m.records()} - before
            self.assertEqual(len(created), 1)
            query = self.p.calls[-1]
            top = {item['ref'] for item in query['context']}
            self.assertTrue(created <= top)
            self.assertTrue(any('UNIQUE_APPRAISAL_REASONING' in item['text'] for item in query['context']))
            self.assertLessEqual(len(top & {r.memory_id for r in self.m.records()}), self.cycle.retrieval_limit)

    def test_new_experience_cannot_be_used_as_pretext_to_reappraise_only_old_sources(self):
        source = self.read(); self.enable()
        self.run_reply(replies=(Reply((rest(),)),))
        from iam.world.cycle import LivingCycle
        self.rebuild(LivingCycle); self.write('Второй опыт'); self.enable()
        self.assertEqual(self.run_reply(self.goal(source)).outcome, 'no_decision')
        self.assertFalse(self.c.state['intentions'])
        self.rebuild(LivingCycle); self.write('Третий опыт', project='third'); self.enable()
        self.assertEqual(self.run_reply(self.interpretation(source)).outcome, 'no_decision')
        self.assertFalse(any(r.text == 'UNIQUE_APPRAISAL_REASONING' for r in self.m.records()))

    def test_normal_choice_keeps_its_interpretation_and_existing_intention_transition(self):
        source = self.read(); self.enable()
        self.assertEqual(self.run_reply(replies=(Reply((self.interpretation(source),)),)).outcome, 'done')
        self.assertEqual(self.run_reply(self.goal(source)).outcome, 'done')
        self.assertEqual(self.run_reply(self.transition('abandoned')).outcome, 'done')
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'abandoned')

    def test_revised_belief_hides_dependent_old_version_from_following_choice(self):
        source = self.read()
        first = self.interpretation(source)
        first['body']['draft']['content']['belief'] = {'belief_id': 'light', 'agreement': .3, 'confidence': .7}
        self.assertEqual(self.run_reply(first).outcome, 'done')
        pointer = self.c.state['beliefs']['light']
        belief = self.m._source(pointer['memory_id'])
        dependent = self.interpretation(belief)
        dependent['body']['draft']['content']['text'] = 'DEPENDENT_OLD_BELIEF'
        self.assertEqual(self.run_reply(dependent).outcome, 'done')
        hidden = next(row.memory_id for row in self.m.records() if row.text == 'DEPENDENT_OLD_BELIEF')
        fresh = self.write('Новое независимое наблюдение'); self.enable()
        revised = self.interpretation(fresh)
        revised['body']['draft']['content']['belief'] = {'belief_id': 'light', 'agreement': -.2, 'confidence': .5}
        self.assertEqual(self.run_reply(revised, replies=(Reply((rest(),)),)).outcome, 'done')
        self.assertEqual(self.c.state['beliefs']['light']['version'], 2)
        query = self.p.calls[-1]
        self.assertNotIn(hidden, json.dumps(query['context']))
        self.assertNotIn('DEPENDENT_OLD_BELIEF', json.dumps(query['context']))

    def test_unknown_outcome_is_visible_as_unknown_and_never_as_confirmed_success(self):
        from iam.interfaces import ActionOutcome
        with patch.object(self.world, 'execute', return_value=ActionOutcome('unknown', 'Исход не установлен')):
            report = self.run_reply(act(WRITE, {'project_id': 'uncertain', 'title': 'Попытка', 'text': 'Нельзя подтвердить'}))
        self.assertEqual(report.actions[0].status, 'unknown_outcome')
        source = self.c.store.get_memory(report.actions[0].memory_id)
        self.enable()
        self.assertEqual(self.run_reply(replies=(Reply((rest(),)),)).outcome, 'done')
        appraisal = self.p.calls[-2]
        value = json.loads(next(item['text'] for item in appraisal['context'] if item['ref'] == source.memory_id))
        self.assertEqual(value['outcome'], 'unknown')
        self.assertNotIn('Подтверждён успех', value['text'])

    def test_assessment_cannot_execute_actions_or_transition_an_existing_goal(self):
        source = self.read()
        self.run_reply(self.goal(source)); self.enable()
        before = set(self.world.state.get('works', {}))
        report = self.run_reply(act(WRITE, {'project_id': 'bad', 'title': 'Не писать', 'text': 'Нет'}))
        self.assertEqual(report.outcome, 'no_decision')
        self.assertEqual(set(self.world.state.get('works', {})), before)
        self.assertEqual(self.p.calls[-1]['purpose'], 'appraise')
        # A different new experience allows another assessment, never another attempt on the old one.
        from iam.world.cycle import LivingCycle
        self.rebuild(LivingCycle)
        self.write('Другой самостоятельный опыт'); self.enable()
        self.assertEqual(self.run_reply(self.transition('abandoned')).outcome, 'no_decision')
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'active')

    def test_second_goal_in_same_cycle_is_rejected_by_request_scope(self):
        source = self.read(); self.enable()
        other = self.goal(source, iid='other')
        self.run_reply(self.goal(source), replies=(Reply((other,)),))
        self.assertIn('my_goal', self.c.state['intentions'])
        self.assertNotIn('other', self.c.state['intentions'])

    def test_error_reserves_basis_and_does_not_repeat_automatically(self):
        self.read(); self.enable()
        self.p.queue(RuntimeError('provider unavailable'))
        report = self.cycle.run(self.due())
        self.assertEqual(report.reason, 'appraisal_provider_error')
        self.assertEqual(self.run_reply(rest()).outcome, 'done')
        self.assertFalse(self.c.state['intentions'])

    def test_stale_paused_malformed_or_tool_answer_never_applies_a_goal(self):
        for mode in ('stale', 'paused', 'malformed', 'tool'):
            with self.subTest(mode=mode):
                # Each subcase has a distinct confirmed experience and fresh reservation.
                from iam.world.cycle import LivingCycle
                self.rebuild(LivingCycle)
                source = self.write('Новый опыт ' + mode, project=mode)
                self.enable()
                goal = self.goal(source, iid=mode)
                original = ScriptedProvider.propose
                def change(provider, request):
                    response = original(provider, request)
                    if mode == 'stale': response['state_version'] -= 1
                    if mode == 'paused': self.c.pause_session('operator')
                    if mode == 'malformed': return None
                    if mode == 'tool': response['proposals'][0]['body']['tool_calls'] = []
                    return response
                with patch.object(ScriptedProvider, 'propose', change):
                    report = self.run_reply(goal)
                self.assertEqual(report.outcome, 'no_decision')
                self.assertNotIn(mode, self.c.state['intentions'])
                if self.c.session_state == 'paused': self.c.resume_session()

    def test_refused_request_or_reservation_stops_before_provider(self):
        self.read(); self.enable()
        with patch.object(self.c, 'record_request', return_value=SimpleNamespace(event=None)):
            report = self.cycle.run(self.due())
        self.assertEqual(report.reason, 'appraisal_request_rejected')
        self.assertEqual(len(self.p.calls), 1)
        original = self.c.record_event
        def fail_marker(spec, **kwargs):
            if spec.type == 'proposal.assessed': return SimpleNamespace(event=None)
            return original(spec, **kwargs)
        with patch.object(self.c, 'record_event', fail_marker):
            report = self.cycle.run(self.due())
        self.assertEqual(report.reason, 'appraisal_reservation_rejected')
        self.assertEqual(len(self.p.calls), 1)
