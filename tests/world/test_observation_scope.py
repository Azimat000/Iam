import json

from iam.stubs import rest
from .helpers import LivingCase


class ObservationScopeTest(LivingCase):
    def test_own_story_and_read_material_confirm_text_operations_only(self):
        received = self.read()
        written = self.write('Меня позвали из дальней комнаты. Я ответила.')
        self.run_reply(rest())
        context = self.p.calls[-1]['context']
        rows = {item['ref']: json.loads(item['text']) for item in context if item['layer'] == 'personal'}
        self.assertEqual(rows[received.memory_id]['observation_scope'], 'read_text_only')
        self.assertEqual(rows[written.memory_id]['observation_scope'], 'created_text_only')
        catalog = next(json.loads(item['text']) for item in context if '"world_results"' in item['text'])
        result = next(row for row in catalog['world_results'] if row['memory_id'] == written.memory_id)
        self.assertEqual(result['observation_scope'], 'created_text_only')
        self.assertEqual(result['scope'], 'artifact')
        self.assertEqual(written.origin, 'observation')
        self.assertEqual(self.m.observation_scope(self.m.prepare(kind='semantic', origin='own_hypothesis', text='Мысль', sources=(written.memory_id,))), None)
        self.assertTrue(self.m.fade(written.memory_id).applied)
        self.run_reply(rest())
        self.assertNotIn('Меня позвали', json.dumps(self.p.calls[-1], ensure_ascii=False))

