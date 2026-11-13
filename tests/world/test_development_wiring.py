import json
from unittest.mock import patch

from iam.cycle import Cycle
from iam.providers.codex.provider import citation_refs
from iam.stubs import rest
from world.helpers import LivingCase


class DevelopmentWiringTest(LivingCase):
    def test_practice_pairs_have_exact_citable_receipt_and_no_duplicate(self):
        source = self.read()
        self.run_reply(self.goal(source))
        result = self.write()
        self.run_reply(rest())
        request = self.p.calls[-1]
        options = self.cycle.motivation.learning.practice_options(request['context'])
        self.assertEqual(len(options), 1)
        self.assertIn(options[0]['event_id'], citation_refs(request))
        self.assertIn(options[0]['result_ref'], citation_refs(request))
        receipt = next(json.loads(i['text']) for i in request['context'] if i['ref'] == result.sources[0])
        self.assertEqual(receipt['outcome'], 'confirmed')
        self.assertEqual(receipt['result_ref'], result.memory_id)
        original = Cycle._context
        def with_receipt(cycle, *args, **kwargs):
            context = original(cycle, *args, **kwargs)
            context.append({'ref': result.sources[0], 'origin': 'observation', 'layer': 'state',
                            'text': 'Уже предъявленное подтверждение'})
            return context
        with patch.object(Cycle, '_context', with_receipt):
            self.run_reply(rest())
        self.assertEqual(sum(i['ref'] == result.sources[0] for i in self.p.calls[-1]['context']), 1)

    def test_forgotten_result_does_not_leave_receipt_or_practice_option(self):
        source = self.read()
        self.run_reply(self.goal(source))
        result = self.write()
        self.assertTrue(self.m.fade(result.memory_id).applied)
        self.run_reply(rest())
        request = self.p.calls[-1]
        self.assertNotIn(result.sources[0], citation_refs(request))
        self.assertEqual(self.cycle.motivation.learning.practice_options(request['context']), [])
