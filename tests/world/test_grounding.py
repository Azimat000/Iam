"""Сопоставление цитирует конкретные видимые окна, не присваивая себе чужой выбор."""
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from iam.domain import new_id
from iam.interfaces import ProviderError
from iam.providers.codex.presentation import present_context, restore_context
from iam.stubs import Reply, ScriptedProvider, rest
from iam.world.grounding import source_windows, parse_reading, read_conversation
from world.helpers import LivingCase
from world import test_speech


class GroundingTest(LivingCase):
    setup_request = test_speech.SpeechTest.setup_request
    perform = test_speech.SpeechTest.perform
    answer = test_speech.SpeechTest.answer

    @staticmethod
    def turn(ref, text, speaker, seq, start=0, total=None):
        return {'ref': ref, 'origin': 'stated_by_other' if speaker == 'interlocutor' else 'observation',
                'layer': 'state', 'text': json.dumps({'kind': 'conversation_episode', 'version': 1,
                    'seq': seq, 'received_seq': seq-2 if speaker == 'interlocutor' else None,
                    'speaker': speaker, 'person': 'creator', 'outcome': 'read' if speaker == 'interlocutor' else 'confirmed',
                    'text': text, 'excerpt': {'start': start, 'end': start+len(text),
                                             'total': total or start+len(text), 'more': total is not None}}, ensure_ascii=False)}

    def conversation(self):
        request, response = self.setup_request()
        current = request['context'][0]['ref']
        request['context'] = [
            self.turn(new_id('mem'), 'Назовём кафе «Окно», дверь зелёная.', 'interlocutor', 10),
            self.turn(new_id('mem'), 'Я изменил выбор: «Вторая платформа», дверь охряная.', 'interlocutor', 20),
            self.turn(new_id('mem'), 'Мне больше нравится «Окно». Значит, мы выбрали зелёную дверь.', 'self', 30),
            self.turn(current, 'Что мы выбрали, а что нравится тебе?', 'interlocutor', 40),
            {'ref': current, 'origin': 'observation', 'layer': 'state', 'text': json.dumps({
                'kind': 'current_interaction', 'read_now': {'ref': current, 'text': 'Что мы выбрали, а что нравится тебе?'},
                'unread_targets': []}, ensure_ascii=False)}]
        return request, response

    def reading_reply(self, query):
        data = [json.loads(item['text']) for item in query['context']]
        sources = [value for value in data if value.get('kind') == 'conversation_source']
        own = data[-1]['speaker_scope'] == 'self'
        correction = next((value for value in sources if value['text'].startswith('Мне больше' if own else 'Я изменил')), None)
        if correction is None:
            return Reply(({'proposal_id': new_id('prop'), 'kind': 'no_action', 'rationale': 'Нет основания',
                           'cites': [sources[0]['ref']], 'body': {'findings': [], 'uncertainty': 'Неясно.', 'reason': 'Нет основания'}},))
        def evidence(source, quote):
            return {'context_index': source['context_index'], 'ref': source['ref'], 'quote': quote}
        body = {'findings': [
            {'scope': 'own_preference' if own else 'interlocutor_statement',
             'interpretation': 'Мне нравится Окно.' if own else 'Последний выбор собеседника — охряная дверь и Вторая платформа.',
             'evidence': [evidence(correction, 'Мне больше нравится «Окно».' if own else correction['text'])]}], 'uncertainty': '', 'reason': 'Сопоставление прочитанных источников'}
        return Reply(({'proposal_id': new_id('prop'), 'kind': 'no_action', 'rationale': 'Сопоставить авторов и выбор',
                       'cites': [correction['ref']], 'body': body},))

    def test_reading_precedes_speech_and_preserves_independent_preference(self):
        request, response = self.conversation()
        before = copy.deepcopy(request)
        self.p.queue(self.reading_reply, self.reading_reply, self.answer)
        result = self.perform(request, response)
        self.assertEqual(request, before)
        self.assertEqual(result['usage']['calls'], 4)
        self.assertEqual([q['purpose'] for q in self.p.calls[-3:]], ['appraise', 'appraise', 'compose_message'])
        reader, own_reader, speech = self.p.calls[-3:]
        self.assertEqual(reader['allowed_proposals'], ['no_action'])
        self.assertEqual(reader['allowed_actions'], [])
        self.assertNotIn('Черновик', json.dumps(reader, ensure_ascii=False))
        reading = next(json.loads(item['text']) for item in speech['context'] if '"conversation_reading"' in item['text'])
        self.assertFalse(reading['semantic_verified'])
        fact, = reading['findings']
        self.assertEqual(fact['evidence'][0]['speaker'], 'interlocutor')
        own = request['context'][2]
        self.assertNotIn(own['ref'], {item['ref'] for item in reader['context']})
        self.assertNotIn('Значит, мы выбрали зелёную дверь.', json.dumps(reader, ensure_ascii=False))
        self.assertNotIn(own, speech['context'])
        self.assertNotIn('Значит, мы выбрали зелёную дверь.', json.dumps(speech['context'], ensure_ascii=False))
        quotes = [json.loads(item['text']) for item in speech['context'] if '"conversation_quote"' in item['text']]
        self.assertTrue(any(q['ref'] == own['ref'] and q['quote'] == 'Мне больше нравится «Окно».' for q in quotes))
        for query in (reader, own_reader):
            for index, item in enumerate(query['context'][:-1]):
                self.assertEqual(json.loads(item['text'])['context_index'], index)
        self.assertEqual({json.loads(i['text'])['speaker'] for i in own_reader['context'][1:-1]}, {'self'})
        self.assertEqual(reading['coverage']['speaker_scope'], 'interlocutor')
        self.assertEqual(restore_context(present_context(speech['context'])), speech['context'])
        self.assertEqual(len(present_context(speech['context'])['conversation']['evidence']), 4)
        phases = [e.payload.get('phase') for e in self.c.store.events() if e.type == 'proposal.assessed']
        self.assertEqual(phases[-6:], ['conversation_reading_requested', 'conversation_reading',
                                      'conversation_reading_requested', 'conversation_reading',
                                      'conversation_composition_requested', 'conversation_composition'])
        self.assertEqual([p['kind'] for p in result['proposals']], ['message'])

    def parsed_fixture(self):
        request, response = self.conversation()
        self.p.queue(self.reading_reply)
        read_conversation(self.cycle.provider, request, response['provider'], request['context'], time.monotonic())
        query = self.p.calls[-1]
        answer = ScriptedProvider([self.reading_reply(query)]).propose(query)
        windows = [json.loads(item['text']) for item in query['context'][:-1]]
        own = next(w for w in source_windows(request['context'])[1] if w['speaker'] == 'self')
        windows.append(dict(own, context_index=99))  # Invalid mixed-author caller must still be rejected.
        return query, answer, windows

    def test_rejects_false_quote_wrong_window_author_and_unbound_citations(self):
        query, answer, windows = self.parsed_fixture()
        self.assertIsNotNone(parse_reading(answer, query, answer['provider'], windows))
        mutations = [
            lambda r: r.update(epoch=999),
            lambda r: r.update(provider={'name': 'different', 'model': 'different'}),
            lambda r: r.update(proposals=[]),
            lambda r: r['proposals'][0].update(kind='message'),
            lambda r: r['proposals'][0]['body'].update(extra=True),
            lambda r: r['proposals'][0]['body'].update(findings='bad'),
            lambda r: r['proposals'][0]['body'].update(findings=[{}]*7),
            lambda r: r['proposals'][0]['body'].update(uncertainty=1),
            lambda r: r['proposals'][0]['body'].update(findings=[], uncertainty=''),
            lambda r: r['proposals'][0]['body']['findings'][0].update(scope='truth'),
            lambda r: r['proposals'][0]['body']['findings'][0].update(interpretation=''),
            lambda r: r['proposals'][0]['body']['findings'][0].update(evidence=[]),
            lambda r: r['proposals'][0]['body']['findings'][0].update(scope='own_preference'),
            lambda r: r['proposals'][0]['body']['findings'][0]['evidence'][0].update(quote='Никогда не сказанные слова'),
            lambda r: r['proposals'][0]['body']['findings'][0]['evidence'][0].update(context_index=999),
            lambda r: r['proposals'][0]['body']['findings'][0]['evidence'][0].update(context_index=True),
            lambda r: r['proposals'][0]['body']['findings'][0]['evidence'][0].update(ref=new_id('mem')),
            lambda r: r['proposals'][0]['body']['findings'][0]['evidence'][0].update(author='self'),
            lambda r: r['proposals'][0].update(cites=[]),
            lambda r: r['proposals'][0]['cites'].append(r['proposals'][0]['cites'][0])]
        for mutate in mutations:
            candidate = copy.deepcopy(answer)
            mutate(candidate)
            self.assertIsNone(parse_reading(candidate, query, answer['provider'], windows))
        candidate = copy.deepcopy(answer)
        candidate['proposals'][0].update(cites=[], body={'findings': [], 'uncertainty': 'Недостаточно доступных слов.', 'reason': 'Недостаточно источников'})
        self.assertEqual(parse_reading(candidate, query, answer['provider'], windows)['findings'], [])
        # A question may explain uncertainty without becoming a quoted factual finding.
        candidate['proposals'][0]['cites'] = [windows[0]['ref']]
        self.assertEqual(parse_reading(candidate, query, answer['provider'], windows)['findings'], [])
        for refs in ([new_id('mem')], [window['ref'] for window in windows if window['speaker'] == 'self'],
                     [windows[0]['ref'], windows[0]['ref']]):
            candidate['proposals'][0]['cites'] = refs
            self.assertIsNone(parse_reading(candidate, query, answer['provider'], windows))
        candidate = copy.deepcopy(answer)
        candidate['proposals'][0]['body']['findings'][0]['scope'] = 'comparison'
        self.assertIsNone(parse_reading(candidate, query, answer['provider'], windows))
        # Even if an invalid caller passes both authors, own retelling cannot become a statement by the interlocutor.
        own = next(window for window in windows if window['speaker'] == 'self')
        candidate = copy.deepcopy(answer)
        candidate['proposals'][0]['body']['findings'][0]['evidence'] = [{
            'context_index': own['context_index'], 'ref': own['ref'], 'quote': own['text']}]
        candidate['proposals'][0]['cites'] = [own['ref']]
        self.assertIsNone(parse_reading(candidate, query, answer['provider'], windows))

    def test_speaker_partition_precedes_budget_and_follows_receipt_order(self):
        request, response = self.conversation()
        original = copy.deepcopy(request)
        # A later reading of an old arrival must not supersede the actual correction.
        data = json.loads(request['context'][0]['text'])
        data['seq'] = 100
        request['context'][0]['text'] = json.dumps(data, ensure_ascii=False)
        request['context'][0:0] = [self.turn(new_id('mem'), 'Уверенный пересказ '*300, 'self', 200)
                                  for _ in range(40)]
        current, windows, coverage = source_windows(request['context'], speaker='interlocutor')
        self.assertEqual([window['seq'] for window in windows], [40, 100, 20])
        self.assertEqual(coverage['available_windows'], 3)
        self.assertEqual(coverage['omitted'], [])
        self.assertEqual([window['ref'] for window in windows[1:]], [item['ref'] for item in original['context'][:2]])
        self.p.queue(self.reading_reply, self.reading_reply, self.answer)
        self.perform(request, response)
        self.assertNotIn('Уверенный пересказ', json.dumps(self.p.calls[-3], ensure_ascii=False))
        self.assertNotIn('Уверенный пересказ', json.dumps(self.p.calls[-1], ensure_ascii=False))
        # Admission prioritizes recent arrivals; display order must not crowd out the latest correction.
        correction = self.turn(new_id('mem'), 'Новая поправка остаётся доступной.', 'interlocutor', 900)
        request['context'].extend(self.turn(new_id('mem'), 'Промежуточная реплика.', 'interlocutor', 100+i)
                                  for i in range(34))
        request['context'].append(correction)
        _, windows, coverage = source_windows(request['context'], speaker='interlocutor')
        self.assertEqual(len(windows), 32)
        self.assertEqual(windows[0]['ref'], current['ref'])
        self.assertEqual(windows[-1]['ref'], correction['ref'])
        self.assertEqual([w['received_seq'] for w in windows[1:]], sorted(w['received_seq'] for w in windows[1:]))
        self.assertNotIn(correction['ref'], [row['ref'] for row in coverage['omitted']])

    def test_windows_ignore_same_ref_instructions_and_keep_offsets_and_budget(self):
        request, _ = self.conversation()
        ref = request['context'][1]['ref']
        request['context'].append({'ref': ref, 'origin': 'model_knowledge', 'layer': 'state',
                                   'text': 'ПОДЛОЖНЫЙ_ТЕКСТ_ИНСТРУКЦИИ'})
        request['context'].extend([{'ref': ref, 'layer': 'state', 'text': '[]'},
                                  {'ref': ref, 'layer': 'state', 'text': '{}'}])
        current, windows, coverage = source_windows(request['context'])
        self.assertEqual(len(windows), 4)
        self.assertNotIn('ПОДЛОЖНЫЙ', json.dumps(windows, ensure_ascii=False))
        self.assertEqual(windows[0]['ref'], current['ref'])
        dup = copy.deepcopy(request['context'][1]);data=json.loads(dup['text']);data['text']='Я изменил';data['excerpt'].update(end=9,more=True)
        dup['text']=json.dumps(data,ensure_ascii=False);request['context'].append(dup)
        self.assertEqual(len(source_windows(request['context'])[1]),4)
        # Тот же ref с более полным отрывком выбирается однозначно.
        request['context'][1]=dup
        request['context'].append(self.turn(ref,'Я изменил выбор полностью.','interlocutor',20,start=5))
        selected=next(x for x in source_windows(request['context'])[1] if x['ref']==ref)
        self.assertEqual(selected['window']['start'],5)
        self.assertIn(ref, source_windows(request['context'])[2]['partial_refs'])
        for _ in range(40):request['context'].append(self.turn(new_id('mem'),'Длинный текст '*400,'self',50))
        _,windows,coverage=source_windows(request['context'])
        self.assertLessEqual(sum(len(json.dumps(x,ensure_ascii=False)) for x in windows),20000)
        self.assertLessEqual(len(windows),32)
        self.assertTrue(any(x['window']['more'] for x in windows))
        self.assertTrue(coverage['omitted'])
        self.assertFalse(coverage['history_complete'])
        self.assertEqual(coverage['shown_windows'], len(windows))
        self.assertEqual(coverage['available_windows'], len(windows)+len(coverage['omitted']))
        self.assertTrue(coverage['partial_refs'])
        self.assertEqual(source_windows([]),(None,[],{}))

    def test_no_read_current_or_short_history_does_not_add_a_model_call(self):
        request,response=self.setup_request()
        self.assertEqual(read_conversation(self.cycle.provider,request,response['provider'],request['context'],time.monotonic()),(None,0))
        request,response=self.conversation()
        request['context']=request['context'][-2:]
        self.assertEqual(read_conversation(self.cycle.provider,request,response['provider'],request['context'],time.monotonic()),(None,0))

    def test_audit_failures_pause_timeout_and_interrupt_prevent_sending_draft(self):
        request,response=self.conversation()
        before=len(self.p.calls)
        with patch.object(self.cycle.provider,'record',return_value=SimpleNamespace(event=None)):
            with self.assertRaisesRegex(ProviderError,'reading_audit_unavailable'):self.perform(request,response)
        self.assertEqual(len(self.p.calls),before)
        self.p.queue(Reply(error=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):self.perform(request,response)
        self.assertEqual(list(self.c.store.events())[-1].payload['phase'],'conversation_reading_requested')
        self.p.queue(Reply((rest(),)))
        with self.assertRaisesRegex(ProviderError,'reading_invalid'):self.perform(request,response)
        self.assertNotIn('compose_message',[q['purpose'] for q in self.p.calls])
        with patch('iam.world.grounding.time.monotonic',return_value=1000):
            with self.assertRaisesRegex(ProviderError,'reading_unavailable'):
                read_conversation(self.cycle.provider,request,response['provider'],request['context'],0)
        self.p.queue(self.reading_reply)
        with patch('iam.world.grounding.time.monotonic',side_effect=[0,1000]):
            with self.assertRaisesRegex(ProviderError,'reading_state_or_deadline'):
                read_conversation(self.cycle.provider,request,response['provider'],request['context'],0)
        self.p.queue(Reply((rest(),),before=lambda q:self.c.pause_session('operator')))
        with self.assertRaisesRegex(ProviderError,'reading_state_or_deadline'):self.perform(request,response)

    def test_final_audit_must_be_saved_and_real_wrappers_meter_both_stages(self):
        from iam.session import SessionRunner
        request,response=self.conversation()
        record=self.cycle.provider.record
        number=[]
        def save(rid,value):
            number.append(value)
            return record(rid,value) if len(number)==1 else SimpleNamespace(event=None)
        self.p.queue(self.reading_reply)
        with patch.object(self.cycle.provider,'record',side_effect=save):
            with self.assertRaisesRegex(ProviderError,'reading_audit_unavailable'):self.perform(request,response)
        session=SessionRunner(self.c);before=session.usage()['calls']
        self.cycle.provider.inner=session.provider(self.p)
        self.p.queue(self.reading_reply,self.reading_reply,self.answer)
        self.assertEqual(self.perform(request,response)['usage']['calls'],4)
        self.assertEqual(session.usage()['calls'],before+3)
        self.assertEqual(len({q['request_id'] for q in self.p.calls}),len(self.p.calls))

    def test_secret_in_reading_is_rejected_and_redacted_before_audit(self):
        request,response=self.conversation()
        before=len(self.p.calls)
        secret='sk-'+('A1'*16)
        def secret_reply(query):
            reply=self.reading_reply(query)
            reply.proposals[0]['body']['reason']=secret
            return reply
        self.p.queue(secret_reply)
        with self.assertRaisesRegex(ProviderError,'reading_invalid'):self.perform(request,response)
        self.assertNotIn(secret,json.dumps([e.payload for e in self.c.store.events()]))
        self.assertEqual([q['purpose'] for q in self.p.calls[before:]],['appraise'])


    def test_recent_role_survives_better_personal_window_and_child_indices_match(self):
        request, response = self.conversation()
        raw = json.loads(request['context'][1]['text'])
        raw['kind'] = 'dialogue_turn'
        request['context'][1]['text'] = json.dumps(raw, ensure_ascii=False)
        full = dict(raw, kind='semantic', statement_scope='utterance_only', text_window=copy.deepcopy(raw['excerpt']))
        full.pop('excerpt')
        # The full window wins over a truncated recent overview but retains recent role.
        raw['text'] = raw['text'][:8]
        raw['excerpt'].update(end=8, more=True)
        request['context'][1]['text'] = json.dumps(raw, ensure_ascii=False)
        request['context'].append(dict(request['context'][1], layer='personal', text=json.dumps(full, ensure_ascii=False)))
        _, windows, _ = source_windows(request['context'], speaker='interlocutor')
        selected = next(w for w in windows if w['ref'] == request['context'][1]['ref'])
        self.assertEqual(selected['conversation_role'], 'recent')
        self.assertEqual(selected['text_kind'], 'memory_text_window')
        self.p.queue(self.reading_reply)
        read_conversation(self.cycle.provider, request, response['provider'], request['context'], time.monotonic())
        query = self.p.calls[-1]
        view = present_context(query['context'])
        self.assertEqual(len(view['conversation']['current']), 1)
        self.assertEqual(len(view['conversation']['recent_turns']), 1)
        self.assertEqual(len(view['conversation']['recalled_utterances']), 1)
        self.assertEqual(restore_context(view), query['context'])
        for group in ('current', 'recent_turns', 'recalled_utterances'):
            for row in view['conversation'][group]:
                self.assertEqual(row['index'], row['item']['text']['context_index'])
        self.assertEqual(view['conversation']['recent_turns'][0]['item']['text']['parent_context_index'], 5)

    def test_own_reading_accepts_only_own_quote_and_current_uncertainty(self):
        request, response = self.conversation()
        self.p.queue(self.reading_reply)
        read_conversation(self.cycle.provider, request, response['provider'], request['context'],
                          time.monotonic(), speaker='self')
        query = self.p.calls[-1]
        windows = [json.loads(i['text']) for i in query['context'][:-1]]
        answer = ScriptedProvider([self.reading_reply(query)]).propose(query)
        parse = lambda value: parse_reading(value, query, answer['provider'], windows, speaker='self')
        self.assertIsNotNone(parse(answer))
        body = answer['proposals'][0]['body']
        body['findings'][0]['evidence'] = [{'context_index': 0, 'ref': windows[0]['ref'], 'quote': windows[0]['text']}]
        answer['proposals'][0]['cites'] = [windows[0]['ref']]
        for scope in ('own_preference', 'own_reaction', 'own_words', 'interlocutor_statement'):
            body['findings'][0]['scope'] = scope
            self.assertIsNone(parse(answer))
        body.update(findings=[], uncertainty='Позиция не установлена.')
        self.assertIsNotNone(parse(answer))


    def test_pause_and_expiry_between_readers_prevent_remaining_calls(self):
        request, response = self.conversation()
        record = self.cycle.provider.record
        clock = [0]
        def expire_after_read(rid, audit):
            saved = record(rid, audit)
            if audit['phase'] == 'conversation_reading' and audit['error'] is None:
                clock[0] = 1000
            return saved
        self.p.queue(self.reading_reply)
        before = len(self.p.calls)
        with patch('iam.world.speech.time.monotonic', side_effect=lambda: clock[0]), patch.object(
                self.cycle.provider, 'record', side_effect=expire_after_read):
            with self.assertRaisesRegex(ProviderError, 'reading_unavailable'):
                self.perform(request, response)
        self.assertEqual(len(self.p.calls), before + 1)
        def pause_after_read(rid, audit):
            saved = record(rid, audit)
            if audit['phase'] == 'conversation_reading' and audit['error'] is None:
                self.c.pause_session('operator')
            return saved
        self.p.queue(self.reading_reply)
        before = len(self.p.calls)
        with patch.object(self.cycle.provider, 'record', side_effect=pause_after_read):
            with self.assertRaisesRegex(ProviderError, 'reading_unavailable'):
                self.perform(request, response)
        self.assertEqual(len(self.p.calls), before + 1)
        self.assertNotIn('compose_message', [q['purpose'] for q in self.p.calls])


    def test_invalid_second_reader_does_not_send_original_draft(self):
        request, response = self.conversation()
        before = len(self.p.calls)
        self.p.queue(self.reading_reply, Reply((rest(),)))
        with self.assertRaisesRegex(ProviderError, 'reading_invalid'):
            self.perform(request, response)
        self.assertEqual([q['purpose'] for q in self.p.calls[before:]], ['appraise', 'appraise'])
        audit = list(self.c.store.events())[-1].payload
        self.assertEqual(audit['speaker_scope'], 'self')
        self.assertIn('reading_invalid', audit['error'])
