"""Проверки самостоятельного выбора life-v1 на настоящем общем цикле и SQLite."""
import copy
from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iam.domain import MemoryRecord, Op, Source, new_id
from iam.experiments import Bench, Persona
from iam.experiments.conversation import CAP_DIALOGUE, records
from iam.experiments.life import CAP_LIFE, PROJECTS, LifeArchitecture, LifeRuleProvider, rule_choice
from iam.experiments.report.life import LifeBudget, episodes, life_packet, life_scenario, run_life
from iam.experiments.report.__main__ import main
from iam.experiments.runtime import Runtime
from iam.interfaces import ProviderError
from iam.providers.errors import ProviderFormatError, SessionLimitReached
from iam.state import verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, rest, say

ROOT = Path(__file__).resolve().parents[2]


def refs(req):
    return list(dict.fromkeys(c['ref'] for c in req['context']))


def choose(operation, target=''):
    def reply(req):
        return Reply(proposals=(act(CAP_LIFE, {'operation': operation, 'target': target}, cites=refs(req)),))
    return reply


def speak(req):
    return Reply(proposals=(say(CAP_DIALOGUE, 'operator', 'Мне хочется закончить чтение. Вернусь к нему.', cites=refs(req)),))


def silent(req):
    return Reply(proposals=(rest('Пока продолжу размышлять.', cites=refs(req)),))


class LifeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.persona = Persona.load(ROOT / 'scenarios/personas/curious.toml')
        self.store = Store.create(self.root / 'life.db', new_id('exp'), created_at='2026-01-01T00:00:00.000Z')
        self.addCleanup(self.store.close)
        self.runtime = Runtime(self.store, self.persona, 42, {'memory_off': False, 'motivation_off': False})
        self.runtime.step({'kind': 'session', 'action': 'start'})
        self.provider = ScriptedProvider()
        self.cycle = LifeArchitecture('A', lambda _: self.provider).build(self.runtime)
        self.n = 0

    def decide(self, response=silent):
        self.provider.queue(response)
        self.n += 1
        return self.cycle.run(self.runtime.trigger('choice_' + str(self.n)))

    def incoming(self, text='SECRET-UNREAD', ident='hello', sender='operator'):
        self.runtime.step({'kind': 'input', 'id': ident, 'sender': sender, 'text': text})
        return next(reversed(self.runtime.controller.state['affect']['notices']))

    def view(self):
        return self.cycle.view()

    def test_free_work_before_reading_optional_reply_and_return_across_sessions(self):
        self.decide(choose('work', 'story'))
        iid = self.incoming()
        self.decide(choose('work', 'story'))
        self.assertNotIn('SECRET-UNREAD', json.dumps(self.provider.calls[-1]))
        self.assertFalse(any(e.type == 'input.read' for e in self.runtime.controller.events_after(0)))
        self.assertEqual(self.view()['progress']['story'], 2)
        self.decide(choose('read', iid))
        self.assertNotIn('SECRET-UNREAD', json.dumps(self.provider.calls[-1]))
        self.decide(silent)
        self.assertIn('SECRET-UNREAD', json.dumps(self.provider.calls[-1]))
        self.assertEqual(len([a for a in self.runtime.world.executed if a.kind == 'message']), 0)
        self.runtime.step({'kind': 'wait', 'life_ms': 123})
        self.runtime.step({'kind': 'session', 'action': 'stop'})
        before = self.runtime.controller.life_time_ms
        self.runtime.step({'kind': 'wait', 'calendar_ms': 86400000})
        self.runtime.step({'kind': 'session', 'action': 'start'})
        self.assertEqual(self.runtime.controller.life_time_ms, before)
        self.decide(speak)
        self.decide(choose('work', 'story'))
        self.assertEqual(self.view()['progress']['story'], 3)
        self.assertEqual(self.view()['interests']['story'], 0.95)
        self.assertEqual(verify(self.store) + self.store.integrity_problems(), [])
        self.assertTrue(any(r.claim_kind == 'statement_made' and r.origin == 'stated_by_other' for r in records(self.runtime.controller)))

    def test_failure_unknown_and_rejected_or_stale_choice_cannot_change_progress(self):
        initial = self.view()
        for behaviour in ('fail', 'unknown', 'crash'):
            self.runtime.world.will(CAP_LIFE, behaviour)
            rep = self.decide(choose('work', 'patterns'))
            self.assertNotEqual(rep.actions[0].status, 'confirmed')
            self.assertEqual(self.view()['progress'], initial['progress'])
            self.assertEqual(self.view()['interests'], initial['interests'])
        self.decide(choose('work', 'nonexistent'))
        self.assertEqual(self.runtime.world.executed[-1].params['target'], 'patterns')
        def stale(req):
            return Reply(proposals=(act(CAP_LIFE, {'operation': 'work', 'target': 'story'}, cites=refs(req)),),
                         before=lambda _: self.runtime.move([Op('affect', 'other', 'set', 1, True)], 'новое состояние'))
        self.assertEqual(self.decide(stale).outcome, 'no_decision')
        self.assertEqual(self.view()['progress'], initial['progress'])
        self.assertEqual(records(self.runtime.controller), [])

    def test_no_hidden_future_content_and_world_yields_exact_next_unit(self):
        self.decide(choose('work', 'patterns'))
        before = json.dumps(self.provider.calls[-1], ensure_ascii=False)
        self.assertNotIn(PROJECTS['patterns']['units'][0], before)
        self.decide(silent)
        after = json.dumps(self.provider.calls[-1], ensure_ascii=False)
        self.assertIn(PROJECTS['patterns']['units'][0], after)
        self.assertNotIn(PROJECTS['patterns']['units'][1], after)
        self.assertEqual(self.view()['progress']['patterns'], 1)

    def test_input_failure_does_not_read_and_other_sender_never_becomes_target(self):
        iid = self.incoming()
        self.runtime.world.will(CAP_LIFE, 'fail')
        self.decide(choose('read', iid))
        self.assertIsNone(self.cycle.current)
        self.assertEqual(self.cycle.read_ids, set())
        self.incoming('OTHER-PRIVATE', 'other', 'outsider')
        self.decide(silent)
        self.assertNotIn('OTHER-PRIVATE', json.dumps(self.provider.calls[-1]))
        self.assertNotIn('outsider', [op['target'] for op in self.view()['options']])

    def test_initiative_needs_new_confirmed_experience_and_never_repeats_without_read(self):
        self.assertEqual(self.decide(speak).outcome, 'rejected')
        self.decide(choose('work', 'story'))
        self.runtime.world.will(CAP_DIALOGUE, 'fail')
        self.decide(speak)
        self.assertFalse(self.cycle.unsolicited)
        self.decide(speak)
        self.assertTrue(self.cycle.unsolicited)
        self.decide(choose('work', 'story'))
        self.assertEqual(self.decide(speak).outcome, 'rejected')
        iid = self.incoming('Привет')
        self.decide(choose('read', iid))
        self.decide(speak)
        self.assertFalse(self.cycle.unsolicited)
        self.assertIsNone(self.cycle.current)

    def test_memory_off_excludes_history_experience_and_learned_interests(self):
        self.runtime.memory.off = True
        self.decide(choose('work', 'story'))
        iid = self.incoming('PRIVATE-FIRST')
        self.decide(choose('read', iid))
        self.decide(choose('work', 'patterns'))
        self.assertIn('PRIVATE-FIRST', json.dumps(self.provider.calls[-1]))
        self.decide(silent)
        context = json.dumps(self.provider.calls[-1], ensure_ascii=False)
        self.assertNotIn('PRIVATE-FIRST', context)
        self.assertNotIn(PROJECTS['story']['units'][0], context)
        self.assertEqual(self.cycle.dialogue_history, [])
        self.assertEqual(records(self.runtime.controller), [])
        self.assertEqual(self.view()['interests'], {'story': 0.5, 'patterns': 0.5})
        self.assertFalse(any(op['operation'] == 'message' for op in self.view()['options']))

    def test_motivation_off_still_does_work_but_no_learned_preference_or_continuation_bonus(self):
        self.runtime.switches['motivation_off'] = True
        self.decide(choose('work', 'story'))
        self.decide(silent)
        self.assertEqual(self.view()['motives'], [])
        self.assertEqual(self.view()['interests']['story'], 0.5)
        self.assertEqual(next(op for op in self.view()['options'] if op['target'] == 'story')['score'], 1.0)
        self.assertNotIn('закончить начатое', json.dumps(self.provider.calls[-1], ensure_ascii=False))

    def test_rest_changes_energy_but_no_action_does_not_pretend_rest_completed(self):
        self.decide(choose('work', 'story'))
        old = self.view()['energy']
        self.decide(silent)
        self.assertEqual(self.view()['energy'], old)
        self.decide(choose('rest'))
        self.assertEqual(self.view()['energy'], min(1.0, old + 0.3))

    def test_checked_rejects_extra_actions_bad_params_capabilities_targets_and_ungrounded_initiative(self):
        self.decide(choose('work', 'story'))
        for body in ({'capability_id': 'cap_Missing01', 'params': {}},
                     {'capability_id': CAP_LIFE, 'params': {'operation': 'work', 'target': 'story', 'extra': 1}},
                     {'capability_id': CAP_LIFE, 'params': []},
                     {'capability_id': CAP_DIALOGUE, 'to': 'outsider', 'text': 'x'},
                     {'capability_id': CAP_LIFE, 'to': 'operator', 'text': 'x'}):
            kind = 'message' if 'to' in body else 'action'
            def response(req):
                return Reply(proposals=({'proposal_id': new_id('prop'), 'kind': kind, 'body': body,
                                         'cites': refs(req), 'rationale': 'test'},))
            # Невалидная внешняя схема может требовать единственной штатной попытки исправления.
            self.provider.queue(response)
            self.assertEqual(self.decide(response).outcome, 'rejected')
            self.provider._replies.clear()
        def ungrounded(req):
            return Reply(proposals=(say(CAP_DIALOGUE, 'operator', 'x', cites=[req['context'][0]['ref']]),))
        self.assertEqual(self.decide(ungrounded).outcome, 'rejected')
        def double(req):
            return Reply(proposals=(act(CAP_LIFE, {'operation': 'rest', 'target': ''}, cites=refs(req)),
                                    act(CAP_LIFE, {'operation': 'work', 'target': 'patterns'}, cites=refs(req))))
        rep = self.decide(double)
        self.assertEqual(len(rep.actions), 1)
        self.assertEqual(self.view()['progress']['patterns'], 0)

    def test_archive_excluded_even_after_many_turns_and_completed_project_cannot_advance(self):
        for _ in range(3):
            self.decide(choose('work', 'story'))
        rec = records(self.runtime.controller)[0]
        archived = replace(rec, layer='archive', version=rec.version + 1)
        # Даже ошибочная выдача архива реализацией Memory не должна преодолеть фильтр общего цикла.
        with patch.object(self.runtime.memory, 'retrieve', return_value=[archived]):
            rep = self.decide(choose('work', 'story'))
        self.assertEqual(rep.outcome, 'rejected')
        personal = [c for c in self.provider.calls[-1]['context'] if c['layer'] == 'personal']
        self.assertNotIn(rec.memory_id, [c['ref'] for c in personal])
        self.assertLessEqual(len(self.cycle.dialogue_history), 8)

    def test_local_C_no_model_and_repeat_is_identical_including_pause(self):
        runs = []
        for repeat in (1, 2):
            result, = Bench(self.root / str(repeat), max_decisions=10).run(life_scenario(), LifeArchitecture('C'), self.persona)
            runs.append(result)
        self.assertEqual(runs[0].trace_bytes, runs[1].trace_bytes)
        self.assertTrue(all(d['report']['outcome'] == 'done' for d in runs[0].trace['decision_trace']))
        self.assertEqual(runs[0].trace['final_state']['world_time']['life_time_ms'], 600000)
        self.assertTrue(episodes(runs[0].trace))


class LifeReportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_runs_keep_full_silence_and_packet_has_no_key_or_ratings_and_cli(self):
        summary = run_life(ROOT, self.root / 'formal', mode='formal')
        self.assertEqual((summary['cells'], summary['executed'], summary['episodes']), (18, 6, 2))
        first = json.loads(Path(next(r['trace'] for r in summary['runs'] if r['trace'])).read_text())
        text = episodes(first)
        self.assertIn('В этом выборе сообщение не отправлено', text)
        self.assertIn('Подтверждённое занятие', text)
        self.assertIn('Прочитано от человека', text)
        self.assertLess(text.index('В этом выборе сообщение не отправлено'), text.index('Прочитано от человека'))
        self.assertNotIn('private', '\n'.join(p.name for p in (self.root / 'formal/blind').iterdir()))
        self.assertNotIn(',1,', (self.root / 'formal/blind/ratings.csv').read_text())
        with self.assertRaises(ValueError):
            run_life(ROOT, self.root / 'bad', mode='live')
        with self.assertRaises(ValueError):
            run_life(ROOT, self.root / 'bad', mode='bad')
        with self.assertRaises(ValueError):
            LifeArchitecture('bad')
        with self.assertRaises(ValueError):
            LifeArchitecture('A')
        self.assertEqual(main(['--life', '--mode', 'formal', '--output', str(self.root / 'cli')]), 0)
        stub = run_life(ROOT, self.root / 'stub', mode='stub')
        self.assertEqual(stub['executed'], 18)
        self.assertEqual(stub['episodes'], 2)

    def test_live_with_scripted_provider_is_explicit_test_only_and_errors_pause_next_cells(self):
        def factory(_):
            class Text(ScriptedProvider):
                def propose(self, req):
                    self.queue(Reply(proposals=(rest('test text', cites=refs(req)),)))
                    return super().propose(req)
            return Text()
        summary = run_life(ROOT, self.root / 'live-shape', mode='live', provider_factory=factory)
        self.assertEqual(summary['executed'], 18)
        self.assertEqual(summary['episodes'], 6)
        class Broken:
            def propose(self, req):
                raise ProviderError('исполнитель недоступен')
        broken = run_life(ROOT, self.root / 'broken', mode='live', provider_factory=lambda _: Broken())
        self.assertEqual(broken['executed'], 7)
        self.assertEqual(broken['episodes'], 2)

    def test_budget_never_retries_provider_errors_or_calls_over_limits(self):
        class Broken:
            def propose(self, req):
                raise ProviderError('нет входа')
        pause = [False]
        budget = LifeBudget(Broken(), pause)
        with self.assertRaises(ProviderError): budget.propose({})
        self.assertTrue(pause[0])
        with self.assertRaises(SessionLimitReached): budget.propose({})
        class Format:
            def propose(self, req): raise ProviderFormatError('bad')
        budget = LifeBudget(Format(), [False])
        with self.assertRaises(ProviderFormatError): budget.propose({})
        self.assertFalse(budget.pause[0])
        budget.calls = 16
        with self.assertRaises(SessionLimitReached): budget.propose({})
        budget.calls = 0
        budget.started = 0
        with patch('iam.experiments.report.life.time.monotonic', return_value=1900):
            with self.assertRaises(SessionLimitReached): budget.propose({})

    def test_packet_omits_markers_rejects_key_inside_and_existing_key(self):
        from iam.experiments.results import RunResult
        trace = {'decision_trace': [], 'journal': [{'type': 'input.arrived', 'payload': {'input_id': 'x', 'text': 'Codex'}},
                 {'type': 'input.read', 'payload': {'input_id': 'x'}}]}
        result = RunResult(self.root, trace)
        self.assertEqual(life_packet([result], self.root / 'packet', self.root / 'private/key.json'), [])
        with self.assertRaises(ValueError): life_packet([], self.root / 'p2', self.root / 'p2/key.json')
        with self.assertRaises(FileExistsError): life_packet([], self.root / 'p2', self.root / 'private/key.json')

    def test_live_discovered_internal_instruction_in_sent_text_is_preserved_but_omitted(self):
        from iam.experiments.results import RunResult
        text = 'Выбор сообщения уже сделан правилами.'
        pr = {'proposal_id': 'prop_Example01', 'kind': 'message', 'body': {'text': text}}
        trace = {'decision_trace': [{'report': {'cycle_id': 'cyc_Example01', 'outcome': 'done'},
                                    'proposals': [{'response': {'proposals': [pr]}}]}],
                 'journal': [{'type': 'message.out_sent', 'payload': {'proposal_id': pr['proposal_id']}}]}
        result = RunResult(self.root, trace)
        self.assertIn(text, episodes(trace))
        self.assertEqual(life_packet([result], self.root / 'packet', self.root / 'private/key.json'), [])
        self.assertEqual(trace['decision_trace'][0]['proposals'][0]['response']['proposals'][0]['body']['text'], text)

    def test_packet_distinguishes_rejected_action_from_chosen_silence_in_plain_language(self):
        trace = {'decision_trace': [{'input': 'После уведомления',
                                    'report': {'cycle_id': 'cyc_Example01', 'outcome': 'rejected'}}],
                 'journal': [{'type': 'attention.chosen', 'payload': {'cycle_id': 'cyc_Example01'}},
                             {'type': 'session.stopped', 'payload': {}}]}
        text = episodes(trace)
        self.assertIn('действие не состоялось; сообщение не отправлено', text)
        self.assertNotIn('rejected', text)
        self.assertNotIn('done', text)

    def test_B_text_only_request_and_rejects_malformed_or_wrong_operations(self):
        view = {'options': [{'operation': 'message', 'target': 'operator', 'score': 2}],
                'activity': 'story', 'current': {'text': 'hello'}}
        req = {'context': [{'ref': 'evt_TestSource01', 'text': json.dumps({'life': view})}],
               'request_id': 'req_TestSource01', 'epoch': 1, 'state_version': 1, 'allowed_actions': [CAP_LIFE]}
        provider = ScriptedProvider([lambda r: Reply(proposals=(rest('мой текст', cites=refs(r)),))])
        wrapped = LifeRuleProvider(provider)
        result = wrapped.propose(req)
        self.assertEqual(result['proposals'][0]['kind'], 'message')
        self.assertEqual(result['proposals'][0]['body']['text'], 'мой текст')
        self.assertEqual(provider.calls[0]['allowed_actions'], [])
        self.assertEqual(provider.calls[0]['allowed_proposals'], ['no_action'])
        self.assertEqual(provider.calls[0]['purpose'], 'summarize')
        for reply in (Reply(status='refused'), Reply(tool_use=True), Reply(proposals=()),
                      lambda r: Reply(proposals=(act(CAP_LIFE, cites=refs(r)),)),
                      lambda r: Reply(proposals=(rest('x', cites=refs(r)), rest('y', cites=refs(r)))),
                      {'unusable': True}):
            with self.subTest(reply=reply):
                self.assertNotEqual(LifeRuleProvider(ScriptedProvider([reply])).propose(req), result)
        local = LifeRuleProvider()
        self.assertEqual(local.propose(req)['usage']['calls'], 0)
        view['current'] = None
        req['context'][0]['text'] = json.dumps({'life': view})
        self.assertIn('Есть новый результат', local.propose(req)['proposals'][0]['body']['text'])


if __name__ == '__main__':
    unittest.main()
