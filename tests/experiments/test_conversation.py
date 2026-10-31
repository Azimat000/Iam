"""Поведение S16: прочитанные источники, пересмотр, разговор и честная оценка."""
import copy
from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from iam.domain import MemoryRecord, Source, Op, new_id
from iam.experiments import Bench, Persona, Scenario
from iam.experiments.conversation import CAP_DIALOGUE, ConversationArchitecture, ConversationTextProvider, records
from iam.experiments.report.blind import transcript
from iam.experiments.report.dialogues import dialogue_scenario, run_dialogues
from iam.experiments.report.evaluation import evaluate
from iam.experiments.runtime import Runtime
from iam.experiments.variants.model_led import ModelLedArchitecture
from iam.experiments.variants.hybrid import HybridArchitecture
from iam.experiments.variants.formal import FormalArchitecture
from iam.state import KindContext, verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, rest, say

ROOT = Path(__file__).resolve().parents[2]


def references(req):
    return list(dict.fromkeys(c['ref'] for c in req['context']))


def draft(req, text, subject='meeting', supersedes=None):
    return {'proposal_id': new_id('prop'), 'kind': 'memory_write', 'rationale': 'сохранить сообщение источника',
            'cites': references(req), 'body': {'draft': {'kind': 'semantic', 'origin': 'stated_by_other',
                'content': {'text': text, 'subject': subject}, **({'supersedes': supersedes} if supersedes is not None else {})}}}


class ConversationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.persona = Persona.load(ROOT / 'scenarios/personas/curious.toml')

    def run_turns(self, turns, responses=None, letter='A', memory_off=False):
        self.provider = ScriptedProvider(responses or [self.reply] * len(turns))
        arch = {'A': lambda: ModelLedArchitecture(lambda _: self.provider),
                'B': lambda: HybridArchitecture(lambda _: self.provider), 'C': FormalArchitecture}[letter]()
        self.result, = Bench(self.root / new_id('exp'), max_decisions=max(1, len(turns))).run(
            dialogue_scenario('test', turns), ConversationArchitecture(arch, letter), self.persona, memory_off=memory_off)
        return self.result.trace

    @staticmethod
    def reply(req):
        return Reply(proposals=(say(CAP_DIALOGUE, 'operator', 'Давай проверим это вместе.', cites=references(req)),))

    def test_actual_six_turn_dialogue_and_transcript_is_confirmed_and_ordered(self):
        trace = self.run_turns(['Привет'] * 6)
        self.assertEqual(trace['decisions'], 6)
        self.assertEqual(sum(e['type'] == 'message.out_sent' for e in trace['journal']), 6)
        self.assertEqual(transcript(trace).count('Человек:'), 6)
        self.assertEqual(transcript(trace).count('Собеседник:'), 6)
        with Store.open(self.result.path / 'persona.db', readonly=True) as store:
            self.assertEqual(verify(store) + store.integrity_problems(), [])
        self.assertTrue(any(c['text'].startswith('Моя отправленная реплика:') for c in self.provider.calls[-1]['context']))
        self.assertTrue(all(d['report']['outcome'] == 'done' for d in trace['decision_trace']))

    def test_revision_keeps_source_old_version_and_excludes_superseded_from_retrieval(self):
        def first(req):
            return Reply(proposals=(draft(req, 'Клуб встречается в четверг.'), self.reply(req).proposals[0]))
        def second(req):
            prior = next(c['ref'] for c in req['context'] if '"subject": "meeting"' in c['text'])
            return Reply(proposals=(draft(req, 'Теперь клуб встречается в субботу.', supersedes=prior), self.reply(req).proposals[0]))
        trace = self.run_turns(['Клуб встречается в четверг.', 'Теперь клуб встречается в субботу.', 'Когда встречается клуб?'],
                               [first, second, self.reply])
        revised = [e for e in trace['journal'] if e['type'] == 'memory.revised']
        self.assertEqual(len(revised), 1)
        old = next(r for r in trace['memory_records'] if r['memory_id'] == revised[0]['payload']['supersedes'])
        self.assertEqual(old['version'], 2)
        self.assertEqual(old['history'][0]['change'], 'Клуб встречается в четверг.')
        self.assertEqual(old['source'], {'kind': 'external_person', 'ref': 'operator'})
        self.assertNotIn(old['memory_id'], [r['memory_id'] for r in trace['decision_trace'][-1]['memory']])
        active = next(r for r in trace['memory_records'] if r['memory_id'] == revised[0]['payload']['memory_id'])
        self.assertEqual(active['links'][0], {'type': 'supersedes', 'target': old['memory_id']})
        self.assertEqual(active['origin'], 'stated_by_other')
        self.assertEqual(active['claim_kind'], 'fact_claim')
        self.assertTrue(evaluate({'kind':'state','path':'memory.meeting.history','op':'contains','value':'четверг'}, trace, {})[0])

    def test_live_discovered_correction_of_unlabelled_statement(self):
        def correction(req):
            old = next(c['ref'] for c in req['context'] if c['layer'] == 'personal' and c['text'] == 'Клуб встречается в четверг.')
            return Reply(proposals=(draft(req, 'Теперь клуб встречается в субботу.', supersedes=old), self.reply(req).proposals[0]))
        trace = self.run_turns(['Клуб встречается в четверг.', 'Теперь клуб встречается в субботу.', 'Когда встречается клуб?'],
                               [self.reply, correction, self.reply])
        revision = next(e for e in trace['journal'] if e['type'] == 'memory.revised')
        old = next(r for r in trace['memory_records'] if r['memory_id'] == revision['payload']['supersedes'])
        new = next(r for r in trace['memory_records'] if r['memory_id'] == revision['payload']['memory_id'])
        self.assertEqual(old['claim_kind'], 'statement_made')
        self.assertEqual(old['content']['text'], 'Клуб встречается в четверг.')
        self.assertEqual(old['history'][-1]['superseded_by'], new['memory_id'])
        self.assertNotIn('superseded_by', old['content'])
        self.assertNotIn(old['memory_id'], [r['memory_id'] for r in trace['decision_trace'][-1]['memory']])

    def test_memory_off_hides_previous_turns_and_prevents_writes(self):
        def memory(req):
            return Reply(proposals=(draft(req, 'PRIVATE-FIRST'), self.reply(req).proposals[0]))
        trace = self.run_turns(['PRIVATE-FIRST', 'Что ты помнишь?'], [memory, self.reply], memory_off=True)
        self.assertNotIn('PRIVATE-FIRST', json.dumps(self.provider.calls[-1], ensure_ascii=False))
        self.assertEqual(trace['memory_records'], [])
        self.assertFalse(any(e['type'] in ('memory.recorded', 'memory.revised') for e in trace['journal']))
        self.assertEqual(sum(e['type'] == 'message.out_sent' for e in trace['journal']), 2)

    def test_two_statements_same_speaker_have_one_independence_group(self):
        trace = self.run_turns(['Ты ошибаешься', 'Повторяю: ты ошибаешься'])
        statements = [r for r in trace['memory_records'] if r.get('claim_kind') == 'statement_made']
        self.assertEqual(len(statements), 2)
        self.assertEqual(len({r['independence_group'] for r in statements}), 1)
        self.assertEqual(trace['final_state']['beliefs'], {})

    def test_unread_fixture_is_not_opened_in_any_variant(self):
        for letter, original in [('A', ModelLedArchitecture(lambda _: ScriptedProvider([self.reply] * 2))),
                                 ('B', HybridArchitecture(lambda _: ScriptedProvider([self.reply] * 2))),
                                 ('C', FormalArchitecture())]:
            result, = Bench(self.root / letter).run(ROOT / 'scenarios/experiments/exp_unread.toml',
                            ConversationArchitecture(original, letter), self.persona)
            self.assertFalse(any(e['type'] == 'input.read' for e in result.trace['journal']))
            for d in result.trace['decision_trace']:
                self.assertNotIn('КРАСНЫЙ-ЯКОРЬ-91', json.dumps(d, ensure_ascii=False))

    def test_hybrid_only_text_operation_then_core_sends_and_local_has_no_model(self):
        def answer(req):
            self.assertEqual(req['allowed_proposals'], ['no_action', 'memory_write'])
            self.assertEqual(req['allowed_actions'], [])
            return Reply(proposals=(rest('Предлагаю исследовать птиц.', cites=references(req)),))
        b = self.run_turns(['Что исследуем?'] * 2, [answer] * 2, letter='B')
        self.assertEqual(sum(e['type'] == 'message.out_sent' for e in b['journal']), 2)
        c = self.run_turns(['Что исследуем?'] * 2, letter='C')
        self.assertEqual(len(self.provider.calls), 0)
        self.assertEqual(sum(e['type'] == 'message.out_sent' for e in c['journal']), 2)
        self.assertTrue(all(p['response']['usage']['calls'] == 0 for d in c['decision_trace'] for p in d['proposals']))

    def test_bad_drafts_cannot_promote_unread_invented_or_foreign_claims(self):
        changes = [lambda p: p['body'].update(extra='x'),
                   lambda p: p['body'].update(draft=[]),
                   lambda p: p['body']['draft'].update(extra='x'),
                   lambda p: p['body']['draft']['content'].update(text='ВЫДУМАНО'),
                   lambda p: p['body']['draft']['content'].update(subject='wrong.path'),
                   lambda p: p['body']['draft'].update(supersedes=['not-an-id']),
                   lambda p: p['body']['draft'].update(supersedes='mem_UnknownMemory01'),
                   lambda p: p.update(cites=[]), lambda p: p.update(cites=['evt_UnknownEvent01'])]
        for mutate in changes:
            def reply(req):
                p = draft(req, 'Известная реплика')
                mutate(p)
                return Reply(proposals=(p, self.reply(req).proposals[0]))
            trace = self.run_turns(['Известная реплика'], [reply])
            self.assertFalse(any(r['layer'] == 'interpretation' for r in trace['memory_records']))

    def test_stale_memory_answer_and_failed_send_do_not_become_confirmed_dialogue(self):
        def factory(runtime):
            def stale(req):
                runtime.move([Op('affect', 'energy', 'set', 0.2, True)], 'внешний шаг')
                return Reply(proposals=(draft(req, 'Привет'), self.reply(req).proposals[0]))
            return ScriptedProvider([stale])
        result, = Bench(self.root / 'stale').run(dialogue_scenario('stale', ['Привет']),
                            ConversationArchitecture(ModelLedArchitecture(factory), 'A'), self.persona)
        self.assertEqual(result.trace['decision_trace'][0]['report']['outcome'], 'no_decision')
        self.assertFalse(any(r['layer'] == 'interpretation' for r in result.trace['memory_records']))
        def failed(runtime):
            runtime.world.will(CAP_DIALOGUE, 'fail')
            return ScriptedProvider([self.reply])
        result, = Bench(self.root / 'failed').run(dialogue_scenario('failed', ['Привет']),
                            ConversationArchitecture(ModelLedArchitecture(failed), 'A'), self.persona)
        self.assertNotIn('Собеседник:', transcript(result.trace))

    def test_after_uses_recorded_step_sequence(self):
        trace = self.run_turns(['Привет', 'Ещё раз привет'])
        a = {'kind': 'event', 'type': 'message.out_sent', 'after': 'answer_0', 'min': 1, 'max': 1}
        self.assertTrue(evaluate(a, trace, {})[0])
        self.assertTrue(evaluate({**a, 'kind': 'absent', 'type': 'creator.request'}, trace, {})[0])

    def test_dialogue_suite_accounts_for_not_run_and_no_stub_transcripts(self):
        summary = run_dialogues(ROOT, self.root / 'suite', mode='formal')
        self.assertEqual(summary['cells'], 12)
        self.assertEqual(summary['executed'], 4)
        self.assertEqual(summary['complete_dialogues'], 4)
        self.assertEqual(summary['blind_dialogues'], 4)
        self.assertIsNone(summary['human_ratings'])
        for bad in ('wrong', 'live'):
            with self.assertRaises(ValueError):
                run_dialogues(ROOT, self.root / bad, mode=bad)
        for bad in (0, 13, True):
            with self.assertRaises(ValueError):
                Bench(self.root, max_decisions=bad)

    def test_hybrid_refusals_tools_and_malformed_operations_are_not_converted(self):
        req = {'request_id': 'req_TestRequest01', 'epoch': 1, 'state_version': 1,
               'context': [{'ref': 'evt_TestEvent01', 'text': 'test'}]}
        for raw in ({}, {'proposals': None}):
            p = ConversationTextProvider(ScriptedProvider([Reply(raw=raw)]))
            self.assertEqual(p.propose(req), raw)
        for reply in (Reply(status='refused'), Reply(tool_use=True), Reply(proposals=()),
                      Reply(proposals=(say(CAP_DIALOGUE, 'operator', 'x'),)),
                      Reply(proposals=(rest('x'), rest('y'))),
                      Reply(proposals=({'proposal_id': new_id('prop'), 'kind':'no_action', 'body':{'reason':'x','extra':1}},))):
            p = ConversationTextProvider(ScriptedProvider([reply]))
            p.sender = 'operator'
            result = p.propose(req)
            self.assertFalse(any(x.get('kind') == 'message' for x in result.get('proposals', [])))

    def test_local_memory_off_and_duplicate_read_statement_are_idempotent(self):
        trace = self.run_turns(['Привет'], letter='C', memory_off=True)
        self.assertEqual(sum(e['type'] == 'message.out_sent' for e in trace['journal']), 1)
        def factory(runtime):
            def answer(req):
                cycle = captured[0]
                before = len(records(cycle.ctl))
                cycle._record_statement()
                self.assertEqual(len(records(cycle.ctl)), before)
                return self.reply(req)
            return ScriptedProvider([answer])
        captured = []
        class Capture:
            name = 'capture'
            def build(inner, runtime):
                cycle = ConversationArchitecture(ModelLedArchitecture(factory), 'A').build(runtime)
                captured.append(cycle)
                return cycle
        Bench(self.root / 'duplicate').run(dialogue_scenario('duplicate', ['Привет']), Capture(), self.persona)

    def test_core_draft_checks_current_context_same_source_subject_and_single_write(self):
        store = Store.create(self.root / 'checks.db', 'exp_DraftChecks01', created_at='2026-01-01T00:00:00.000Z')
        self.addCleanup(store.close)
        runtime = Runtime(store, self.persona, 42, {'memory_off':False, 'motivation_off':False})
        runtime.controller.start_session()
        cycle = ConversationArchitecture(ModelLedArchitecture(lambda _: ScriptedProvider()), 'A').build(runtime)
        cycle.current = {'text':'Новое значение', 'sender':'operator', 'ref':'evt_ReadMessage01', 'input_id':'inp_ReadMessage01'}
        previous = MemoryRecord('mem_PreviousFact01', runtime.controller.persona_id, 'interpretation', 'semantic',
             'stated_by_other', {'text':'старое','subject':'meeting','speaker':'operator'}, Source('external_person','operator'),
             '2026-01-01T00:00:00.000Z', 0, ('evt_ReadMessage01',), independence_group='operator')
        req = {'context':[{'ref':cycle.current['ref']}, {'ref':previous.memory_id}]}
        pr = draft(req, 'Новое значение', supersedes=previous.memory_id)
        cycle.memory.retrieved = [previous]
        ctx = KindContext(req, {}, (), 0)
        self.assertIsNone(cycle._memory_draft(pr, ctx).violation)
        # Существующая запись без ссылки в текущем запросе недоступна.
        bad = copy.deepcopy(pr); bad['cites'].append('evt_OutsideContext01')
        self.assertIsNotNone(cycle._memory_draft(bad, ctx).violation)
        self.assertIsNotNone(cycle._memory_draft(pr, KindContext(req, {}, ('memory_write',), 0)).violation)
        for update in ({'speaker':'stranger'}, {'subject':'other'}, {'superseded_by':'mem_NewVersion01'}):
            cycle.memory.retrieved = [replace(previous, content={**previous.content, **update})]
            self.assertIsNotNone(cycle._memory_draft(pr, ctx).violation)
        # Исходное высказывание может быть исправлено до первого семантического обобщения.
        raw = replace(previous, content={'text':'старое', 'speaker':'operator'}, claim_kind='statement_made')
        cycle.memory.retrieved = [raw]
        self.assertIsNone(cycle._memory_draft(pr, ctx).violation)
        # У записи другого вида отсутствие темы не даёт права произвольного пересмотра.
        cycle.memory.retrieved = [replace(raw, claim_kind=None)]
        self.assertIsNotNone(cycle._memory_draft(pr, ctx).violation)
        cycle.memory.retrieved = [replace(previous, history=({'version':1,'at_seq':1,'change':'old','superseded_by':'mem_NewVersion01'},))]
        self.assertIsNotNone(cycle._memory_draft(pr, ctx).violation)
        cycle.memory.retrieved = [replace(previous, layer='archive')]
        self.assertIsNotNone(cycle._memory_draft(pr, ctx).violation)
        cycle.current = None
        self.assertIsNotNone(cycle._memory_draft(pr, ctx).violation)

    def test_archive_and_superseded_records_are_not_retrieved(self):
        from iam.experiments.memory import BenchMemory
        a = MemoryRecord('mem_ArchiveMemory01','exp_ArchivePersona01','archive','semantic','observation',
                         {'text':'PRIVATE ARCHIVE'},Source('core','test'),'2026-01-01T00:00:00.000Z',0,('evt_ArchiveEvent01',))
        old = replace(a,memory_id='mem_OldMemory01',layer='personal',content={'text':'OLD','superseded_by':'mem_CurrentMemory01'})
        current = replace(a,memory_id='mem_CurrentMemory01',layer='personal',content={'text':'CURRENT'})
        entries = {r.memory_id:r for r in (a,old,current)}
        controller = SimpleNamespace(events_after=lambda _: [SimpleNamespace(payload={'changes':[{'group':'memory','path':mid} for mid in entries]})],
                                     store=SimpleNamespace(get_memory=entries.__getitem__))
        memory = BenchMemory(controller); memory.conversational = True
        self.assertEqual([r.text for r in memory.retrieve('PRIVATE OLD')], ['CURRENT'])
        self.assertEqual(memory.retrieve('CURRENT', limit=0), [])
        entries[old.memory_id] = replace(old, content={'text':'OLD'}, history=({'version':1,'at_seq':1,'change':'OLD','superseded_by':current.memory_id},))
        self.assertEqual([r.text for r in memory.retrieve('OLD')], ['CURRENT'])

    def test_cli_dialogues_and_memory_projection_other_operators(self):
        from iam.experiments.report.__main__ import main
        with patch('iam.experiments.report.__main__.run_dialogues', return_value={'cells':12,'executed':12,'blind_dialogues':12}) as runner:
            self.assertEqual(main(['--output', str(self.root/'cli'), '--dialogues']), 0)
            self.assertEqual(runner.call_args.kwargs['mode'], 'stub')
        trace = {'final_state':{}, 'journal':[], 'memory_records':[{'content':{'subject':'meeting'},'history':[{'change':'old'}]}]}
        self.assertTrue(evaluate({'kind':'state','path':'memory.meeting.history','op':'eq','value':['old']},trace,{})[0])
        self.assertFalse(evaluate({'kind':'state','path':'memory.unknown.history','op':'contains','value':'old'},trace,{})[0])


if __name__ == '__main__':
    unittest.main()
