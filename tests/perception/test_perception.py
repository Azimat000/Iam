import copy
from dataclasses import replace
import json
from unittest.mock import patch

from iam.cycle import Cycle, Runner
from iam.cycle.runner import input_id_for
from iam.domain import Op
from iam.memory import PersistentMemory
from iam.perception import Perception
from iam.state import EventSpec, verify
from iam.stubs import Reply, ScriptedProvider, StubCapabilities, StubCreatorBridge, StubWorld, attend, rest

from state.helpers import StateCase


class PerceptionCase(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)
        self.memory = PersistentMemory(self.c)
        self.provider = ScriptedProvider()
        self.world = StubWorld()
        self.creator = StubCreatorBridge()
        self.cycle = Cycle(self.c, self.provider, self.memory, self.world, StubCapabilities(), self.creator)
        self.p = self.cycle.perception
        self.runner = Runner(self.cycle, self.c, self.creator)

    def arrive(self, text='СКРЫТЫЙ_ТЕКСТ', number=1):
        iid = input_id_for('telegram', str(number))
        result = self.c.record_event(EventSpec('input.arrived', f'rx:telegram:{number}',
                    {'input_id': iid, 'sender': '123', 'channel': 'telegram', 'text': text},
                    event_id='evt_' + iid.removeprefix('inp_')))
        return iid, result.event

    def run_reply(self, trigger, proposal):
        self.provider.queue(Reply((proposal,)))
        return self.cycle.run(trigger)

    def read(self, text='СКРЫТЫЙ_ТЕКСТ', number=1):
        iid, trigger = self.arrive(text, number)
        report = self.run_reply(trigger, attend('dialogue', iid))
        self.assertEqual(report.outcome, 'done', report)
        return iid, trigger


class PerceptionTest(PerceptionCase):
    def test_three_stages_and_first_request_has_no_content(self):
        iid, trigger = self.read()
        events = [e for e in self.c.events_after(0) if e.type.startswith('input.')]
        self.assertEqual([e.type for e in events], ['input.arrived', 'input.noticed', 'input.read'])
        self.assertLess(events[0].seq, events[1].seq)
        self.assertLessEqual(events[1].life_time_ms, events[2].life_time_ms)
        self.assertNotIn('СКРЫТЫЙ_ТЕКСТ', json.dumps(self.provider.calls[0], ensure_ascii=False))
        self.assertEqual(len(self.memory.records()), 1)
        record = self.memory.records()[0]
        self.assertEqual((record.origin, record.claim_kind), ('stated_by_other', 'fact_claim'))
        self.assertEqual(record.sources, (events[2].event_id,))
        self.assertEqual(self.p.notices(), {'count': 0, 'items': []})
        self.provider.queue(Reply((rest(),)))
        self.cycle.run(events[2])
        self.assertIn('СКРЫТЫЙ_ТЕКСТ', json.dumps(self.provider.calls[-1], ensure_ascii=False))
        self.assertIsNone(self.p.notice(iid))
        self.assertEqual(verify(self.c.store), [])

    def test_read_is_not_replayed_after_forgetting_or_memory_off(self):
        iid, _ = self.read()
        read = self.c.find_event(key='rd:' + iid)
        record = self.memory.records()[0]
        self.assertTrue(self.memory.fade(record.memory_id).applied)
        self.provider.queue(Reply((rest(),)))
        self.cycle.run(read)
        self.assertNotIn('СКРЫТЫЙ_ТЕКСТ', json.dumps(self.provider.calls[-1], ensure_ascii=False))
        self.assertEqual(self.memory.records(), [])
        self.memory.off = True
        self.read('ВЫКЛЮЧЕННЫЙ_ТЕКСТ', 2)
        self.assertEqual(self.memory.records(), [])
        self.provider.queue(Reply((rest(),)))
        self.cycle.run(self.c.find_event(key='rd:' + input_id_for('telegram', '2')))
        self.assertNotIn('ВЫКЛЮЧЕННЫЙ_ТЕКСТ', json.dumps(self.provider.calls[-1], ensure_ascii=False))

    def test_levels_whitelist_and_upgrade(self):
        iid, _ = self.arrive()
        self.assertEqual(self.p.notices(), {'count': 0, 'items': []})
        self.p.level = 'N1'
        self.assertTrue(self.p.notice(iid).applied)
        self.assertEqual(self.p.notices(), {'count': 1})
        self.assertEqual(self.p.notices('N2'), {'count': 1, 'items': []})
        item = json.loads(self.p.context()[0]['text'])
        self.assertEqual(set(item), {'notice_level', 'count'})
        self.assertIsNone(self.p.notice(iid))
        self.p.level = 'N2'
        self.assertTrue(self.p.notice(iid).applied)
        notice = self.p.notices()['items'][0]
        self.assertEqual(set(notice), {'input_id', 'sender', 'received_at', 'channel'})
        self.assertNotIn('СКРЫТЫЙ_ТЕКСТ', json.dumps(self.p.context(), ensure_ascii=False))
        self.p.level = 'N1'
        self.assertIsNone(self.p.notice(iid))
        self.assertEqual(len([e for e in self.c.events_after(0) if e.type == 'input.noticed']), 2)

    def test_notice_limit_and_invalid_parameters(self):
        for level, limit in [('N3', 1), ('N1', 0), ('N2', True)]:
            with self.assertRaises(ValueError):
                Perception(self.c, self.memory, level=level, limit=limit)
        with self.assertRaises(ValueError):
            self.p.notices('N3')
        with self.assertRaises(ValueError):
            self.p.notice('inp_Unknown0001')
        self.p.limit = 1
        for n in range(2):
            iid, _ = self.arrive(number=n)
            self.p.notice(iid)
        self.assertEqual(self.p.notices()['count'], 2)
        self.assertEqual(len(self.p.notices()['items']), 1)
        self.assertEqual(len(self.p.context()), 1)

    def test_pause_arrival_does_not_notice_or_read(self):
        self.c.pause_session('operator')
        iid, trigger = self.arrive()
        self.assertEqual(self.runner.run_pending(), [])
        self.assertFalse(self.p.notice(iid).applied)
        self.assertNotIn('input.read', self.types(self.c))
        self.assertEqual(self.provider.calls, [])
        self.c.resume_session()
        self.run_reply(trigger, rest())
        self.assertIn('input.noticed', self.types(self.c))
        self.assertNotIn('input.read', self.types(self.c))

    def test_ignore_is_deliberate_then_can_read_after_restart(self):
        iid, trigger = self.arrive()
        self.run_reply(trigger, rest('Продолжу отдыхать'))
        self.assertEqual(self.p._states()[iid]['stage'], 'ignored')
        self.assertEqual(self.c.state['affect']['attention']['reason'], 'Продолжу отдыхать')
        self.assertEqual(self.world.executed, [])
        self.assertIsNone(self.p.notice(iid))
        self.c.pause_session('operator')
        self.c = self.reopen(self.c)
        self.c.resume_session()
        self.memory = PersistentMemory(self.c)
        self.cycle = Cycle(self.c, self.provider, self.memory, self.world, StubCapabilities(), self.creator)
        self.p = self.cycle.perception
        later = next(e for e in reversed(self.c.events_after(0)) if e.type == 'session.resumed')
        self.run_reply(later, attend('dialogue', iid))
        self.assertEqual(self.p._states()[iid]['stage'], 'read')
        self.assertEqual(self.memory.records()[0].text, 'СКРЫТЫЙ_ТЕКСТ')

    def test_invalid_hidden_or_already_read_reference_rejected(self):
        iid, trigger = self.arrive()
        result = self.run_reply(trigger, attend('event', 'inp_Unknown0001'))
        self.assertEqual(result.outcome, 'rejected')
        self.assertNotIn('input.read', self.types(self.c))
        self.provider.queue(Reply((attend('event', iid),)))
        self.cycle.run(trigger, round_=1)
        self.provider.queue(Reply((attend('dialogue', iid),)))
        self.assertEqual(self.cycle.run(trigger, round_=2).outcome, 'rejected')
        self.assertEqual(self.types(self.c).count('input.read'), 1)

    def test_stale_and_refused_proposals_cannot_open(self):
        iid, trigger = self.arrive()
        self.provider.queue(Reply((attend('dialogue', iid),), before=lambda r: self.c.pause_session('operator')))
        self.cycle.run(trigger)
        self.assertNotIn('input.read', self.types(self.c))
        self.c.resume_session()
        self.provider.queue(Reply((attend('event', iid),), status='refused'))
        self.assertEqual(self.cycle.run(trigger, round_=1).outcome, 'refused')
        self.assertNotIn('input.read', self.types(self.c))

    def test_forged_events_and_stage_changes_rejected(self):
        iid, trigger = self.arrive()
        for stage, key in [('read', 'rd:' + iid), ('noticed', f'ntc:{iid}:N2'), ('ignored', 'ign:' + iid)]:
            res = self.c.apply(self.prop(self.c, Op('preferences', 'x', 'set', 1, True),
                carrier=EventSpec('input.' + stage, key)))
            self.assertFalse(res.applied)
        res = self.c.apply(self.prop(self.c, Op('preferences', 'x', 'set', 1, True),
                          companions=(EventSpec('input.read', 'rd:' + iid),)))
        self.assertFalse(res.applied)
        for path, value in [('world.perception', {}), ('world', {'perception': {iid: {'stage': 'read'}}})]:
            # Сначала создаём настоящее замечание, иначе запись пустого контейнера совпала бы с отсутствием.
            self.p.notice(iid)
            self.assertFalse(self.c.apply(self.prop(self.c, Op('world_time', path, 'set', value, True))).applied)
        with self.assertRaises(ValueError):
            self.c.record_event(EventSpec('input.read', 'rd:' + iid))

    def test_finish_authenticity_and_atomic_memory_failure(self):
        iid, trigger = self.arrive()
        pr = attend('dialogue', iid)
        with self.assertRaises(ValueError):
            self.p.finish(pr, trigger.event_id)
        with patch.object(self.memory, 'write', return_value=type('Result', (), {'applied': False})()):
            self.provider.queue(Reply((pr,)))
            with self.assertRaises(RuntimeError):
                self.cycle.run(trigger)
        self.assertNotIn('input.read', self.types(self.c))
        self.assertEqual(self.p._states()[iid]['stage'], 'noticed')
        accepted = self.c.find_event(key='prop:' + pr['proposal_id'] + ':accept')
        modified = copy.deepcopy(pr)
        modified['rationale'] = 'подмена'
        with self.assertRaises(ValueError):
            self.p.finish(modified, accepted.event_id)
        self.assertTrue(self.p.finish(pr, accepted.event_id).applied)
        self.assertIsNone(self.p.finish(pr, accepted.event_id))
        self.c.pause_session('operator')
        with self.assertRaises(ValueError):
            self.p.finish(pr, accepted.event_id)

    def test_n1_can_open_and_non_input_events_do_not_reveal_text(self):
        self.p.level = 'N1'
        iid, trigger = self.arrive()
        self.run_reply(trigger, attend('event', iid))
        self.assertNotIn('123', json.dumps(self.provider.calls[0]['context'], ensure_ascii=False))
        self.assertEqual(len(self.memory.records()), 1)
        self.assertEqual(self.p._states()[iid]['stage'], 'read')

    def test_pair_hidden_texts_have_identical_visible_context_and_signals(self):
        iid, trigger = self.arrive('ОДИН_СЕКРЕТ')
        self.run_reply(trigger, rest())
        original = self.cycle._context(trigger, self.cycle.attention.choose(trigger, self.memory.decision_state()), [])
        # Та же видимая история; изменяется только транспортный текст, не его метаданные.
        altered = replace(trigger, payload=dict(trigger.payload, text='ДРУГОЙ_СЕКРЕТ'))
        with patch.object(self.p, '_arrivals', return_value={iid: altered}):
            paired = self.cycle._context(altered, self.cycle.attention.choose(altered, self.memory.decision_state()), [])
        self.assertEqual(original, paired)
        self.assertNotIn('СЕКРЕТ', json.dumps(paired, ensure_ascii=False))

    def test_two_actual_databases_match_until_read(self):
        other = PerceptionCase()
        other.setUp()
        self.addCleanup(other.doCleanups)
        iid, trigger = self.arrive('ПЕРВАЯ_ТАЙНА')
        oid, otr = other.arrive('СОВСЕМ_ДРУГАЯ_ТАЙНА')
        self.run_reply(trigger, rest())
        other.run_reply(otr, rest())
        self.assertEqual(iid, oid)
        self.assertEqual(self.provider.calls[0]['context'], other.provider.calls[0]['context'])
        self.assertEqual(self.c.state['affect'], other.c.state['affect'])
        self.provider.queue(Reply((attend('dialogue', iid),)))
        other.provider.queue(Reply((attend('dialogue', oid),)))
        self.cycle.run(trigger, round_=1)
        other.cycle.run(otr, round_=1)
        self.assertNotEqual(self.memory.records()[0].text, other.memory.records()[0].text)

    def test_no_events_no_model_and_action_is_still_free(self):
        self.assertEqual(self.runner.run_pending(), [])
        self.assertEqual(self.provider.calls, [])
        from iam.interfaces import Capability
        from iam.stubs import act
        self.cycle.capabilities = StubCapabilities([Capability('cap_Notes00001', 'заметка')])
        self.cycle = Cycle(self.c, self.provider, self.memory, self.world, self.cycle.capabilities, self.creator)
        self.p = self.cycle.perception
        iid, trigger = self.arrive()
        self.run_reply(trigger, act('cap_Notes00001'))
        self.assertEqual(len(self.world.executed), 1)
        self.assertNotIn('input.read', self.types(self.c))

    def test_new_notice_is_not_blocked_by_ignored_queue(self):
        self.p.limit = 1
        first, trigger = self.arrive(number=20)
        self.run_reply(trigger, rest())
        second, trigger = self.arrive(number=21)
        report = self.run_reply(trigger, attend('dialogue', second))
        self.assertEqual(report.outcome, 'done')
        self.assertEqual(self.p._states()[first]['stage'], 'ignored')
        self.assertEqual(self.p._states()[second]['stage'], 'read')

    def test_old_accepted_cannot_open_after_new_session_or_version_change(self):
        iid, trigger = self.arrive()
        pr = attend('dialogue', iid)
        with patch.object(self.memory, 'write', return_value=type('Result', (), {'applied': False})()):
            self.provider.queue(Reply((pr,)))
            with self.assertRaises(RuntimeError):
                self.cycle.run(trigger)
        accepted = self.c.find_event(key='prop:' + pr['proposal_id'] + ':accept')
        self.c.pause_session('operator')
        self.c.resume_session()
        with self.assertRaises(ValueError):
            self.p.finish(pr, accepted.event_id)
        self.assertNotIn('input.read', self.types(self.c))
        self.c.stop_session()
        self.c.start_session()
        with self.assertRaises(ValueError):
            self.p.finish(pr, accepted.event_id)

    def test_rejected_read_never_saves_content(self):
        self.c.add_validator('test_no_read', lambda check: ['нет'] if check.carrier_type == 'input.read' else [])
        iid, trigger = self.arrive()
        self.assertEqual(self.run_reply(trigger, attend('event', iid)).outcome, 'failed')
        self.assertNotIn('input.read', self.types(self.c))
        self.assertEqual(self.memory.records(), [])

    def test_ignored_is_not_rewritten_on_each_choice(self):
        iid, trigger = self.arrive()
        self.run_reply(trigger, rest())
        self.provider.queue(Reply((rest(),)))
        self.cycle.run(trigger, round_=1)
        self.assertEqual(self.types(self.c).count('input.ignored'), 1)

    def test_malformed_focus_history_and_custom_policy_are_safe(self):
        from iam.cycle.attention import AttentionPolicy
        for value in (3, {'focus':'activity','switch_history':42}, {'switch_history':[True, -1, 0, 'bad']}):
            self.assertTrue(self.c.apply(self.prop(self.c, Op('affect','attention','set',value,True))).applied)
            iid, trigger = self.arrive(number=30 + self.c.state_version)
            self.run_reply(trigger, rest())
        cycle = Cycle(self.c, self.provider, self.memory, self.world, StubCapabilities(), self.creator,
                      attention=AttentionPolicy())
        iid, trigger = self.arrive(number=999)
        self.provider.queue(Reply((rest(),)))
        self.assertEqual(cycle.run(trigger).outcome, 'done')
        self.assertNotIn('choices', json.dumps(self.provider.calls[-1]['context']))
        runner = Runner(cycle, self.c, self.creator, triggers=('input.arrived',))
        self.assertEqual(runner.triggers, ('input.arrived',))

    def test_refused_notice_stops_before_provider_and_refused_ignore_is_visible(self):
        self.c.add_validator('test_no_notice', lambda check: ['нет'] if check.carrier_type == 'input.noticed' else [])
        iid, trigger = self.arrive()
        self.assertEqual(self.cycle.run(trigger).outcome, 'failed')
        self.assertEqual(self.provider.calls, [])
        self.c._validators = [(name, fn) for name, fn in self.c._validators if name != 'test_no_notice']
        self.c.add_validator('test_no_ignore', lambda check: ['нет'] if check.carrier_type == 'input.ignored' else [])
        self.assertEqual(self.run_reply(trigger, rest()).outcome, 'failed')
        self.assertEqual(self.p._states()[iid]['stage'], 'noticed')

    def test_old_ignore_decision_cannot_be_applied_in_new_session(self):
        iid, trigger = self.arrive()
        pr = rest()
        self.c.add_validator('no_ignore', lambda check: ['нет'] if check.carrier_type == 'input.ignored' else [])
        self.assertEqual(self.run_reply(trigger, pr).outcome, 'failed')
        accepted = self.c.find_event(key='prop:' + pr['proposal_id'] + ':accept')
        self.c._validators = [(name, fn) for name, fn in self.c._validators if name != 'no_ignore']
        self.c.stop_session()
        self.c.start_session()
        with self.assertRaises(ValueError):
            self.p.finish(pr, accepted.event_id)
        self.assertEqual(self.p._states()[iid]['stage'], 'noticed')

    def test_notice_upgrade_keeps_existing_ignore_without_duplicate(self):
        self.p.level = 'N1'
        iid, trigger = self.arrive()
        self.run_reply(trigger, rest())
        self.p.level = 'N2'
        self.provider.queue(Reply((rest(),)))
        self.assertEqual(self.cycle.run(trigger, round_=1).outcome, 'done')
        self.assertEqual(self.p._states()[iid], {'stage':'ignored','level':'N2'})
        self.assertEqual(self.types(self.c).count('input.ignored'), 1)
        self.provider.queue(Reply((attend('dialogue', iid),)))
        self.assertEqual(self.cycle.run(trigger, round_=2).outcome, 'done')
        self.assertEqual(self.p._states()[iid]['stage'], 'read')

    def test_legacy_arrival_without_id_is_not_a_crash_or_content_leak(self):
        for n, ref in enumerate((None, 123, 'invalid.path')):
            marker = 'СТАРЫЙ_СКРЫТЫЙ_ТЕКСТ_' + str(n)
            event = self.c.record_event(EventSpec('input.arrived', 'rx:legacy:' + str(n),
                {'text':marker, **({'input_id':ref} if ref is not None else {})})).event
            iid = self.p.input_id(event)
            self.assertTrue(iid.startswith('inp_'))
            self.assertNotIn('.', iid)
            self.run_reply(event, rest())
            self.assertNotIn(marker, json.dumps(self.provider.calls[-1],ensure_ascii=False))
            self.provider.queue(Reply((attend('event', iid),)))
            self.assertEqual(self.cycle.run(event,round_=1).outcome, 'done')
            self.assertEqual(self.memory.records()[-1].text, marker)
