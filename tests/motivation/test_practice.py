import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

from iam.domain import new_id
from iam.stubs import Reply, rest
from world.helpers import LivingCase


def practice(result, *, iid='my_goal', skill='image_to_line'):
    return {'proposal_id': new_id('prop'), 'kind': 'memory_write', 'rationale': 'Проверить свой способ',
            'cites': [result.memory_id, *result.sources], 'body': {'draft': {
                'kind': 'semantic', 'origin': 'own_hypothesis', 'content': {
                    'text': 'Хочу понять, получились ли связанные строки',
                    'practice': {'intention_id': iid, 'result_ref': result.memory_id, 'skill_id': skill}}}}}


class PracticeTest(LivingCase):
    def prepared(self, text='Свет у окна.\nВесна отражается на столе.'):
        source = self.read()
        self.run_reply(self.goal(source))
        return self.write(text)

    def samples(self):
        return [row for row in self.m.records() if 'skill_assessment' in row.content]

    def test_practice_records_bound_assessment_and_keeps_original_goal_active(self):
        result = self.prepared()
        original = copy.deepcopy(self.c.state['intentions']['my_goal'])
        before = len(self.p.calls)
        report = self.run_reply(practice(result), replies=(self.assessment(), self.assessment('evaluation')))
        self.assertEqual(report.outcome, 'done')
        self.assertEqual(len(self.p.calls) - before, 3)
        self.assertEqual(self.c.state['intentions']['my_goal'], original)
        row, = self.samples()
        value = row.content['skill_assessment']
        self.assertEqual((value['format'], value['basis'], value['skill_id'], value['project_id']),
                         (2, 'practice', 'image_to_line', 'poem'))
        self.assertEqual(value['verdict'], 'pass')
        self.assertEqual(row.origin, 'model_explanation')
        self.assertEqual(row.sources[0], result.memory_id)
        self.assertEqual(self.c.find_event(event_id=value['assessment_event']).payload['phase'], 'practice')
        self.assertFalse(value['independent_observation'])
        self.run_reply(rest())
        context = self.p.calls[-1]['context']
        options = self.cycle.motivation.learning.practice_options(context)
        self.assertEqual(options[0]['assessed_skills'], {'image_to_line': row.memory_id})
        hidden = [item for item in context if item['ref'] != row.memory_id]
        self.assertNotIn('assessed_skills', self.cycle.motivation.learning.practice_options(hidden)[0])

    def test_failure_uncertainty_and_scope_mismatch_are_not_success(self):
        result = self.prepared()
        for verdict, kind in (('fail', 'artifact'), ('uncertain', 'artifact'), ('pass', 'independent_observation')):
            replies = (self.assessment(kind=kind),)
            if kind == 'artifact':
                replies += (self.assessment('evaluation', verdict=verdict),)
            self.run_reply(practice(result), replies=replies)
            result = self.write('Следующая попытка ' + verdict + kind)
        values = [row.content['skill_assessment'] for row in self.samples()]
        self.assertEqual([value['verdict'] for value in values], ['fail', 'uncertain', 'uncertain'])
        self.assertFalse(values[-1]['scope_match'])
        self.assertEqual(self.cycle.motivation.learning.measurements()['image_to_line']['estimate'], 0)
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'active')

    def test_done_has_priority_regardless_of_proposal_order(self):
        result = self.prepared()
        before = len(self.p.calls)
        report = self.run_reply(practice(result), self.transition('done', result=result),
                                replies=(self.assessment(), self.assessment('evaluation')))
        self.assertEqual(len(self.p.calls) - before, 3)
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'done')
        row, = self.samples()
        self.assertNotIn('basis', row.content['skill_assessment'])
        self.assertEqual(row.content['skill_assessment']['skill_id'], 'poem')
        self.assertIn('assessment_slot_used', next(stage for stage in report.stages
                      if stage.stage == 'verification').detail['rejected'].values())

    def test_one_practice_slot_and_no_repeated_assessment_after_restart(self):
        result = self.prepared()
        before = len(self.p.calls)
        self.run_reply(practice(result), practice(result, skill='other_method'),
                       replies=(self.assessment(), self.assessment('evaluation')))
        self.assertEqual(len(self.p.calls) - before, 3)
        self.assertEqual(len(self.samples()), 1)
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        from iam.stubs import StubCreatorBridge
        from iam.world import PersistentWorld
        from iam.world.cycle import LivingCycle, WorldMemory
        self.m, self.world = WorldMemory(self.c), PersistentWorld(self.c)
        self.cycle = LivingCycle(self.c, self.p, self.m, self.world, self.caps, StubCreatorBridge())
        self.c.resume_session()
        before = len(self.p.calls)
        self.run_reply(practice(result))
        self.assertEqual(len(self.p.calls) - before, 1)
        self.assertEqual(len(self.samples()), 1)
        from iam.state.replay import verify
        self.assertEqual(verify(self.c.store), [])

    def test_invalid_shapes_and_forged_assessment_never_reach_evaluator(self):
        result = self.prepared()
        variants = []
        for key, value in (('skill_id', 'bad.key'), ('intention_id', 'missing'), ('extra', 1)):
            pr = practice(result)
            pr['body']['draft']['content']['practice'][key] = value
            variants.append(pr)
        for change in ({'origin': 'observation'}, {'kind': 'procedural'}, {'extra': True}):
            pr = practice(result)
            pr['body']['draft'].update(change)
            variants.append(pr)
        pr = practice(result)
        pr['body']['draft']['content']['text'] = ''
        variants.append(pr)
        pr = practice(result)
        pr['body']['draft']['content']['practice'] = []
        variants.append(pr)
        forged = practice(result)
        forged['body']['draft']['content'] = {'text': 'Я уже умею', 'skill_assessment': {'verdict': 'pass'}}
        variants.append(forged)
        for pr in variants:
            before = len(self.p.calls)
            self.run_reply(pr)
            self.assertEqual(len(self.p.calls) - before, 1)
            self.assertFalse(self.samples())

    def test_unrelated_malformed_memory_does_not_crash_completion_dispatch(self):
        from iam.intentions.completion import practice_requested
        for body in ({}, {'draft': None}, {'draft': {}}, {'draft': {'content': []}}):
            self.assertFalse(practice_requested({'kind': 'memory_write', 'body': body}))

    def test_ordinary_interpretation_keeps_its_existing_handler_without_review(self):
        result = self.prepared()
        proposal = practice(result)
        proposal['cites'] = [result.memory_id]
        proposal['body']['draft'].update(kind='emotional_associative', content={
            'text': 'Эта попытка заинтересовала меня',
            'appraisal': {'valence': 0.3, 'arousal': 0.4, 'relevance': 0.7, 'regulation': 0.5}})
        before = len(self.p.calls)
        self.assertEqual(self.run_reply(proposal).outcome, 'done')
        self.assertEqual(len(self.p.calls) - before, 1)
        self.assertIn('emotion', self.c.state['affect'])
        self.assertFalse(self.samples())

    def test_early_foreign_forgotten_revised_and_off_results_are_rejected(self):
        early = self.read()
        self.run_reply(self.goal(early, project=self.material, operation='read'))
        before = len(self.p.calls)
        self.run_reply(practice(early))
        self.assertEqual(len(self.p.calls) - before, 1)
        self.assertFalse(self.samples())
        self.run_reply(self.transition('abandoned'))
        self.run_reply(self.goal(early, iid='write_goal'))
        own = self.write()
        other = self.write('Чужой проект по отношению к цели', project='different')
        for result in (early, other):
            before = len(self.p.calls)
            self.run_reply(practice(result, iid='write_goal'))
            self.assertEqual(len(self.p.calls) - before, 1)
        self.cycle.motivation.off = True
        self.run_reply(practice(own, iid='write_goal'))
        self.cycle.motivation.off = False
        self.assertTrue(self.m.revise(own.memory_id, 'Пересмотр результата', sources=own.sources, reason='Ошибка').applied)
        self.run_reply(practice(own, iid='write_goal'))
        self.m.fade(own.memory_id)
        self.run_reply(practice(own, iid='write_goal'))
        self.assertFalse(self.samples())

    def test_partial_and_hidden_event_cannot_be_assessed(self):
        result = self.prepared()
        self.run_reply(rest())
        request = copy.deepcopy(self.p.calls[-1])
        partial = copy.deepcopy(request)
        for item in partial['context']:
            try:
                value = json.loads(item['text'])
            except ValueError:
                continue
            if isinstance(value, dict):
                for shown in value.get('world_results', []):
                    if shown['memory_id'] == result.memory_id:
                        shown['text'] = shown['text'][:5]
                        shown['result_complete'] = False
                item['text'] = json.dumps(value, ensure_ascii=False)
        with self.assertRaises(ValueError):
            self.cycle.motivation.learning.practice_candidate(practice(result), partial)
        self.assertFalse(self.samples())
        request['context'] = [item for item in request['context'] if item['ref'] not in result.sources]
        with self.assertRaises(ValueError):
            self.cycle.motivation.learning.practice_candidate(practice(result), request)
        self.assertNotIn(result.memory_id, [option['result_ref'] for option in
                         self.cycle.motivation.learning.practice_options(request['context'])])

    def test_unsaved_reservation_and_review_do_not_create_assessment(self):
        result = self.prepared()
        before = len(self.p.calls)
        with patch.object(self.c, 'record_event', wraps=self.c.record_event) as recorder:
            original = recorder._mock_wraps
            recorder.side_effect = lambda spec, *a, **kw: (SimpleNamespace(event=None)
                if spec.payload.get('phase') == 'practice_reserved' else original(spec, *a, **kw))
            self.run_reply(practice(result))
        self.assertEqual(len(self.p.calls) - before, 1)
        self.assertFalse(self.samples())
        with patch.object(self.cycle.provider, 'record', return_value=SimpleNamespace(event=None)):
            self.run_reply(practice(result), replies=(self.assessment(), self.assessment('evaluation')))
        self.assertFalse(self.samples())

    def test_pause_during_review_records_no_assessment_and_does_not_retry(self):
        result = self.prepared()
        self.run_reply(practice(result), replies=(Reply(before=lambda req: self.c.pause_session('operator'),
                                                       proposals=(rest(),)),))
        self.assertFalse(self.samples())
        self.c.resume_session()
        before = len(self.p.calls)
        self.run_reply(practice(result))
        self.assertEqual(len(self.p.calls) - before, 1)
        self.assertFalse(self.samples())

    def test_exhausted_deadline_records_uncertainty_without_extra_model_call(self):
        result = self.prepared()
        before = len(self.p.calls)
        with patch('iam.intentions.completion.time.monotonic', side_effect=[0, 1000]):
            self.run_reply(practice(result))
        self.assertEqual(len(self.p.calls) - before, 1)
        row, = self.samples()
        self.assertEqual(row.content['skill_assessment']['verdict'], 'uncertain')
        self.assertIsNone(self.cycle.motivation.learning.measurements()['image_to_line']['estimate'])

    def test_changed_state_during_evaluation_never_applies_the_old_assessment(self):
        result = self.prepared()
        def evaluation(request):
            reply = self.assessment('evaluation')(request)
            self.c.pause_session('operator')
            self.c.resume_session()
            return reply
        self.run_reply(practice(result), replies=(self.assessment(), evaluation))
        self.assertFalse(self.samples())
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'active')
        self.assertTrue(any(event.payload.get('error') == 'assessment_state_changed'
                            for event in self.c.store.events() if event.type == 'proposal.assessed'))

    def test_stale_choice_is_not_reserved_or_sent_to_review(self):
        result = self.prepared()
        def change_state(request):
            self.c.pause_session('operator')
            self.c.resume_session()
        before = len(self.p.calls)
        self.p.queue(Reply((practice(result),), before=change_state))
        self.cycle.run(self.due())
        self.assertEqual(len(self.p.calls) - before, 2)  # прежний один повтор устаревшего обычного выбора
        self.assertTrue(all(request['purpose'] == 'choose_action' for request in self.p.calls[before:]))
        self.assertFalse(self.samples())
        self.assertFalse(any(event.payload.get('phase') == 'practice_reserved'
                             for event in self.c.store.events() if event.type == 'proposal.assessed'))

    def test_distinct_skill_groups_do_not_erase_each_others_measurement(self):
        result = self.prepared()
        for skill in ('image_to_line', 'contrast'):
            self.run_reply(practice(result, skill=skill), replies=(self.assessment(), self.assessment('evaluation')))
        measurements = self.cycle.motivation.learning.measurements()
        self.assertEqual(set(measurements), {'image_to_line', 'contrast'})
        self.assertTrue(all(len(value['samples']) == 1 for value in measurements.values()))

    def test_practice_options_are_bounded_accessible_and_leave_skill_choice_open(self):
        source = self.read()
        for index in range(5):
            self.run_reply(self.goal(source, iid='g'+str(index), project='p'+str(index)))
            self.write('Работа ' + str(index), project='p'+str(index))
        self.run_reply(rest())
        context = self.p.calls[-1]['context']
        options = self.cycle.motivation.learning.practice_options(context)
        self.assertLessEqual(len(options), 4)
        self.assertTrue(options)
        self.assertTrue(all('skill_id' not in option for option in options))
        repeated = [*context, *context, {'ref': 'evt_TestArray01', 'text': '[]'}]
        self.assertEqual(self.cycle.motivation.learning.practice_options(repeated), options)
        self.assertEqual(self.cycle.motivation.learning.practice_options([]), [])
        self.m.off = True
        self.assertEqual(self.cycle.motivation.learning.practice_options(context), [])

    def test_practice_refuses_appraisal_and_unbound_review(self):
        result = self.prepared()
        self.run_reply(rest())
        request = copy.deepcopy(self.p.calls[-1])
        learning = self.cycle.motivation.learning
        pr = practice(result)
        request['purpose'] = 'appraise'
        with self.assertRaises(ValueError):
            learning.practice_candidate(pr, request)
        request['purpose'] = 'choose_action'
        ctx = SimpleNamespace(request=request)
        self.assertEqual(learning.practice(pr, ctx).violation.code, 'practice_unassessed')
        goal, checked, _ = learning.practice_candidate(pr, request)
        from iam.intentions.completion import digest
        key = digest({'request': request, 'proposal': pr, 'goal': goal})
        self.cycle.intentions.reviews[key] = {'basis': 'practice', 'skill_id': 'wrong', 'result': checked}
        self.assertEqual(learning.practice(pr, ctx).violation.code, 'practice_invalid')
