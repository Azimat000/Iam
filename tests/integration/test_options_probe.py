"""Одинаковое прошлое и вызовы; в контроле варианты не доходят до выбора."""
import json
import unittest
from iam.stubs import ScriptedProvider, Reply, rest
from integration import intention_probe as probe, test_intention_probe as fixtures


class OptionsProbeTest(unittest.TestCase):
    setUp = fixtures.IntentionProbeTest.setUp
    goal = fixtures.IntentionProbeTest.goal

    def test_same_past_and_three_calls_without_automatic_acceptance(self):
        def factory(name, transport):
            return ScriptedProvider([Reply((self.goal(name[0]),), explanation='PRIVATE_OPTION_EXPLANATION'),
                                     Reply(()), Reply((rest(),))])
        result = probe.run_probe(self.source,self.output,protocol='options',provider_factory=factory)
        self.assertEqual((result['status'],result['decisions'],result['model_calls']),('completed',4,0))
        manifest = json.loads((self.output/'manifest.json').read_text())
        self.assertEqual((manifest['max_decisions'],manifest['max_model_calls'],manifest['cell_model_calls']),(4,16,4))
        for story in ('A','E'):
            pair=[]
            for arm in ('options_hidden','options_visible'):
                data=json.loads((self.output/f'cells/{story}-{arm}/result.json').read_text())
                self.assertEqual(data['new_intentions'],{})
                self.assertEqual(len(data['traces']),3)
                generator,commit,choice=[row['request'] for row in data['traces']]
                self.assertEqual([row['purpose'] for row in (generator,commit,choice)],['appraise','appraise','choose_action'])
                packet=json.loads(commit['context'][-1]['text'])
                self.assertEqual(len(commit['context'][-1]['text']),5000)
                self.assertEqual(len(packet['options']),int(arm=='options_visible'))
                self.assertNotIn('PRIVATE_OPTION_EXPLANATION',json.dumps(commit))
                self.assertNotIn('hypothetical_future_options',json.dumps(choice))
                self.assertIn('intention',commit['allowed_proposals'])
                pair.append((data,generator,commit))
            self.assertEqual(pair[0][0]['before_digest'],pair[1][0]['before_digest'])
            self.assertEqual(pair[0][1]['allowed_actions'],pair[1][1]['allowed_actions'])
            self.assertEqual([row['text'] for row in pair[0][1]['context']],
                             [row['text'] for row in pair[1][1]['context']])
            self.assertEqual([row['text'] for row in pair[0][2]['context'][:-1]],
                             [row['text'] for row in pair[1][2]['context'][:-1]])
