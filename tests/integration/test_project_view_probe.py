import json
import unittest
from iam.stubs import ScriptedProvider,Reply,rest
from integration import intention_probe as probe,test_intention_probe as fixtures


class ProjectViewProbeTest(unittest.TestCase):
    setUp=fixtures.IntentionProbeTest.setUp

    def test_only_project_projection_differs_between_paired_requests(self):
        def factory(name,transport):
            return ScriptedProvider([Reply(()),Reply((rest(),))])
        result=probe.run_probe(self.source,self.output,protocol='project_view',provider_factory=factory)
        self.assertEqual((result['status'],result['decisions'],result['model_calls']),('completed',4,0))
        for story in ('A','E'):
            entries=[]
            for arm in ('legacy_view','correct_view'):
                data=json.loads((self.output/f'cells/{story}-{arm}/result.json').read_text())
                request=data['traces'][0]['request']
                catalog=next(json.loads(row['text']) for row in request['context'] if row['text'].startswith('{"library"'))
                entries.append((data,catalog,request))
            self.assertEqual(entries[0][0]['before_digest'],entries[1][0]['before_digest'])
            self.assertEqual(entries[0][2]['allowed_proposals'],entries[1][2]['allowed_proposals'])
            self.assertEqual(entries[0][2]['allowed_actions'],entries[1][2]['allowed_actions'])
            for legacy,correct in zip(entries[0][1]['projects'].values(),entries[1][1]['projects'].values()):
                self.assertEqual(legacy['status'],'active')
                self.assertNotIn('status',correct)
                self.assertEqual(correct,{**{k:v for k,v in legacy.items() if k!='status'},'can_edit':True})
            self.assertEqual(entries[0][1]['intentions'],entries[1][1]['intentions'])
            instructions=[next(row['text'] for row in entry[2]['context'] if 'Новые основания этой оценки:' in row['text'])
                          for entry in entries]
            self.assertEqual(instructions[0],instructions[1])
