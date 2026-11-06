import json
import unittest
from unittest.mock import patch

from iam.providers import CodexProvider, CodexSandboxConfig
from iam.providers.codex import fake
from iam.providers.codex.command import SYSTEM_PROMPT
from integration import complete_e, intention_probe as probe, test_intention_probe as fixtures


class CompleteETest(unittest.TestCase):
    setUp = fixtures.IntentionProbeTest.setUp

    def test_only_e_is_selected_and_the_original_story_set_is_restored(self):
        result = complete_e.run_e(self.source, self.output)
        self.assertEqual((result['status'], result['decisions']), ('completed', 3))
        self.assertEqual([c['name'] for c in result['cells']],
                         ['E-baseline', 'E-assessment_only', 'E-prospective'])
        manifest = json.loads((self.output/'manifest.json').read_text())
        self.assertEqual(manifest['stories'], ['E'])
        self.assertIn('tests/integration/complete_e.py', manifest['sources'])
        self.assertEqual(probe.STORIES, ('A', 'E'))
        self.assertFalse((self.output/'initial/A.db').exists())
        with self.assertRaises(ValueError): complete_e.run_e(self.source, self.output)
        self.assertEqual(probe.STORIES, ('A', 'E'))

    def test_wire3_real_provider_path_and_physical_call_accounting(self):
        def factory(name, transport):
            return CodexProvider(transport, CodexSandboxConfig(), max_format_retries=0)
        def runner(argv, stdin, timeout, cleanup):
            query = json.loads(json.loads(stdin)['prompt'][len(SYSTEM_PROMPT+'\n\n'):])
            proposals = ([] if query['purpose']=='appraise' else [{'kind':'no_action',
                          'rationale':'Механическая пауза','cites':[],'body_json':'{"reason":"Пауза"}'}])
            return fake.reply_text(json.dumps({'proposals':proposals,'attention':None,'explanation':'Fixture'}))
        with patch.object(probe, 'subprocess_runner', side_effect=runner) as physical:
            result = complete_e.run_e(self.source, self.output, mode='live',
                                     auth_file=str(self.root/'private.json'), provider_factory=factory)
        self.assertEqual((result['status'], result['model_calls'], physical.call_count), ('completed',5,5))
        self.assertEqual(len(list(self.output.rglob('physical/*.json'))),5)
        self.assertEqual(probe.STORIES, ('A','E'))
