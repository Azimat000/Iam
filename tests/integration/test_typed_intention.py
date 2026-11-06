"""Необязательный формат не создаёт смысл и не обходит прежние барьеры ядра."""
import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from iam.providers import CodexProvider, CodexSandboxConfig
from iam.providers.codex import fake
from iam.providers.codex.classify import classify
from iam.providers.codex.command import SYSTEM_PROMPT, docker_run
from iam.providers.codex.provider import _proposals
from iam.providers.errors import ProviderFormatError
from iam.providers.run import RunResult
from iam.stubs import Reply, act
from iam.world import WRITE
from integration import intention_probe as probe, typed_intention as typed
from integration import test_intention_probe as fixtures
from world.helpers import LivingCase


def envelope(goal=None, proposals=()):
    future = None if goal is None else {key: copy.deepcopy(goal[key]) for key in ("rationale", "cites", "body")}
    return {"proposals": list(proposals), "explanation": "Механическая проверка, не живой ответ",
            "future_intention": future}


class TypedCoreTest(LivingCase):
    def proposals(self, data):
        return _proposals(typed.convert(data))

    def test_explicit_object_equals_legacy_goal_and_null_never_infers_from_work(self):
        goal = self.goal(self.read())
        actual, = self.proposals(envelope(goal))
        self.assertEqual({k: v for k, v in actual.items() if k != 'proposal_id'},
                         {k: v for k, v in goal.items() if k != 'proposal_id'})
        action = {"kind": "action", "rationale": "Хочу позднее вернуться к этому",
                  "cites": goal['cites'], "body_json": json.dumps({"capability_id": WRITE,
                  "params": {"project_id": "poem", "title": "Заметка", "text": "Потом проверю свет"}})}
        self.assertEqual(self.run_reply(*self.proposals(envelope(proposals=[action]))).outcome, 'done')
        self.assertFalse(self.c.state['intentions'])
        self.assertEqual(self.proposals(envelope()), [])

    def test_incomplete_extra_wrong_type_or_forbidden_citations_never_repaired(self):
        good = envelope(self.goal(self.read()))
        variants = []
        for key in good['future_intention']['body']:
            bad = copy.deepcopy(good); del bad['future_intention']['body'][key]; variants.append(bad)
        for key in ('rationale', 'cites', 'body'):
            bad = copy.deepcopy(good); del bad['future_intention'][key]; variants.append(bad)
        for value in ('inp_Notice0001', 'evt_short', 'mem_bad-hyphen'):
            bad = copy.deepcopy(good); bad['future_intention']['cites'] = [value]; variants.append(bad)
        for value in (False, '', [], {'body': {}}):
            bad = copy.deepcopy(good); bad['future_intention'] = value; variants.append(bad)
        bad = copy.deepcopy(good); bad['future_intention']['body']['extra'] = 'invented'; variants.append(bad)
        bad = copy.deepcopy(good); del bad['future_intention']; variants.append(bad)
        bad = copy.deepcopy(good); bad['proposals'] = [{"kind": "unknown"}]; variants.append(bad)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ProviderFormatError):
                typed.convert(data)
        self.assertFalse(self.c.state['intentions'])

    def test_action_plus_goal_preserves_slots_and_two_goals_remain_rejected(self):
        source = self.read()
        action = {"kind": "action", "rationale": "Механический текст", "cites": [],
                  "body_json": json.dumps({"capability_id": WRITE,
                  "params": {"project_id": "poem", "title": "Текст", "text": "Свет у окна"}})}
        report = self.run_reply(*self.proposals(envelope(self.goal(source), [action])))
        self.assertEqual(report.outcome, 'done')
        self.assertEqual(report.actions[0].status, 'confirmed')
        self.assertEqual(self.c.state['intentions']['my_goal']['status'], 'active')
        first = self.goal(source, iid='other')
        second = self.goal(source, iid='third')
        old = {'kind': 'intention', 'rationale': first['rationale'], 'cites': first['cites'],
               'body_json': json.dumps(first['body'])}
        self.run_reply(*self.proposals(envelope(second, [old])))
        self.assertNotIn('third', self.c.state['intentions'])

    def test_hidden_prepared_and_stale_response_do_not_become_own_live_intention(self):
        source = self.read()
        prepared = self.m.prepare(kind='episodic', origin='own_hypothesis', text='Не наблюдение',
                                  sources=(source.memory_id,))
        self.m.write(prepared)
        self.run_reply(*self.proposals(envelope(self.goal(prepared))))
        self.assertFalse(self.c.state['intentions'])
        self.m.off = True
        self.run_reply(*self.proposals(envelope(self.goal(source))))
        self.assertFalse(self.c.state['intentions'])
        self.m.off = False
        proposals = self.proposals(envelope(self.goal(source)))
        # ScriptedProvider callbacks return Reply; change the resulting envelope after provider validation.
        from iam.stubs import ScriptedProvider
        original = ScriptedProvider.propose
        def old_response(provider, request):
            result = original(provider, request)
            result['state_version'] = max(0, request['state_version'] - 1)
            return result
        with patch.object(ScriptedProvider, 'propose', old_response):
            self.run_reply(*proposals, replies=(Reply(tuple(proposals)),))
        self.assertFalse(self.c.state['intentions'])


class TypedTransportTest(unittest.TestCase):
    def test_only_preamble_and_schema_change_and_auth_handoff_survives(self):
        argv = docker_run(CodexSandboxConfig()).argv
        context = {'purpose': 'choose_action', 'context': [{'text': 'AVAILABLE EXPERIENCE'}]}
        payload = {'auth': {'private': 'PRIVATE_SENTINEL'},
                   'prompt': SYSTEM_PROMPT + '\n\n' + json.dumps(context)}
        changed_argv, changed_stdin = typed.prepare_transport(argv, json.dumps(payload))
        decoded = json.loads(changed_stdin)
        self.assertEqual(decoded['auth'], payload['auth'])
        self.assertEqual(decoded['prompt'], typed.PROMPT + '\n\n' + json.dumps(context))
        self.assertEqual(sum(a != b for a, b in zip(argv, changed_argv)), 1)
        self.assertIn(typed.SCHEMA_PATH, changed_argv)
        for command, data in ((argv, '{}'), (argv, json.dumps({**payload, 'prompt': 'wrong'})),
                              (('x', '--output-schema', 'wrong'), json.dumps(payload))):
            with self.assertRaises((KeyError, ValueError)):
                typed.prepare_transport(command, data)
        run = fake.reply_text(json.dumps(envelope()))
        handoff = {'type': 'iam.auth.updated', 'auth': {'private': 'PRIVATE_SENTINEL'}}
        run = replace(run, stdout=run.stdout + json.dumps(handoff) + '\n')
        result = typed.adapt_run(run)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), handoff)
        message = json.loads(result.stdout.splitlines()[2])['item']['text']
        self.assertEqual(json.loads(message), {'proposals': [], 'explanation': envelope()['explanation']})

    def test_malformed_stream_tools_timeout_and_bad_schema_never_repaired(self):
        for run in (fake.tool_attempt(), fake.hang(), fake.error(401, 'failed'),
                    RunResult(0, '{bad'), fake.garbage(), fake.reply_text('[]'),
                    replace(fake.reply_text('{}'), stderr='failure')):
            self.assertEqual(typed.adapt_run(run), run)
        invalid = typed.adapt_run(fake.reply_text('{}'))
        with self.assertRaises(ValueError):
            _proposals(classify(invalid).data)
        duplicate = fake.reply_text('{"proposals":[],"explanation":"x",'
                                    '"future_intention":null,"future_intention":null}')
        self.assertEqual(typed.adapt_run(duplicate), duplicate)

    def test_invalid_typed_envelope_keeps_rotated_auth_for_provider_before_schema_rejection(self):
        handoff = {'type': 'iam.auth.updated', 'auth': {'private': 'ROTATED_PRIVATE_SENTINEL'}}
        for data in ({}, {'proposals': [], 'explanation': 'missing future_intention'},
                     {'proposals': [], 'explanation': 'bad', 'future_intention': {}}):
            run = fake.reply_text(json.dumps(data))
            result = typed.adapt_run(replace(run, stdout=run.stdout + json.dumps(handoff) + '\n'))
            lines = result.stdout.splitlines()
            self.assertEqual(json.loads(lines[-1]), handoff)
            public = replace(result, stdout='\n'.join(lines[:-1]) + '\n')
            with self.assertRaises(ValueError):
                _proposals(classify(public).data)


class TypedProbeTest(unittest.TestCase):
    setUp = fixtures.IntentionProbeTest.setUp
    goal = fixtures.IntentionProbeTest.goal

    def test_real_transport_shape_matrix_saves_original_before_conversion_and_hides_memory(self):
        calls = []
        def factory(name, transport):
            return CodexProvider(transport, CodexSandboxConfig(), max_format_retries=0)
        def physical(argv, stdin, timeout, cleanup):
            calls.append((argv, json.loads(stdin)['prompt']))
            future = None if len(calls) in (3, 6) else envelope(self.goal('A' if len(calls) <= 3 else 'E'))['future_intention']
            if typed.SCHEMA_PATH in argv:
                return fake.reply_text(json.dumps({'proposals': [], 'explanation': 'fixture', 'future_intention': future}))
            return fake.reply([], 'fixture')
        with patch.object(probe, 'subprocess_runner', side_effect=physical):
            result = probe.run_probe(self.source, self.output, mode='live', auth_file=str(self.root / 'private.json'),
                                     provider_factory=factory, protocol='typed')
        self.assertEqual((result['status'], result['model_calls'], result['decisions']), ('completed', 6, 6))
        self.assertEqual([cell['new_intentions'] for cell in result['cells']], [0, 1, 0, 0, 1, 0])
        self.assertEqual([typed.SCHEMA_PATH in call[0] for call in calls], [False, True, True, False, True, True])
        for story, number in (('A', 2), ('E', 5)):
            path = self.output / f'cells/{story}-explicit'
            raw = json.loads((path / f'physical/{number:03d}.json').read_text())['messages'][0]
            self.assertIsNotNone(json.loads(raw)['future_intention'])
            cell = json.loads((path / 'result.json').read_text())
            self.assertEqual(cell['traces'][0]['response']['proposals'][0]['kind'], 'intention')
            off = json.loads((self.output / f'cells/{story}-memory_off/result.json').read_text())
            self.assertNotIn(self.memories[story], json.dumps(off['traces'][0]['request']))
            self.assertEqual(len({c['before_digest'] for c in result['cells'] if c['name'].startswith(story)}), 1)
        manifest = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(manifest['typed_schema'], typed.SCHEMA)
        self.assertEqual(manifest['typed_prompt'], typed.PROMPT)

    def test_typed_live_requires_frozen_image_and_invalid_explicit_answer_stops_without_retry(self):
        with self.assertRaisesRegex(ValueError, 'immutable'):
            probe.run_probe(self.source, self.output, mode='live', auth_file='external', protocol='typed')
        def factory(name, transport):
            return CodexProvider(transport, CodexSandboxConfig(), max_format_retries=0)
        answers = [fake.reply([]), fake.reply_text('{"proposals":[],"explanation":"x","future_intention":{"body":{}}}')]
        with patch.object(probe, 'subprocess_runner', side_effect=answers) as runner:
            result = probe.run_probe(self.source, self.output, mode='live', auth_file=str(self.root / 'private.json'),
                                     provider_factory=factory, protocol='typed')
        self.assertEqual((result['status'], result['model_calls'], runner.call_count), ('stopped', 2, 2))
        self.assertEqual(len(list(self.output.rglob('physical/*.json'))), 2)
        self.assertIn('proposal_schema', (self.output / 'cells/A-explicit/result.json').read_text())
