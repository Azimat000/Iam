import json
from pathlib import Path
import tempfile
import unittest
from iam.providers.codex import fake
from integration.verify_attention_live import verify_attention,TARGET


class AttentionLiveFixtureTest(unittest.TestCase):
    def test_one_real_provider_pass_with_auth_handoff_and_no_core_application(self):
        with tempfile.TemporaryDirectory(prefix='iam-attention-live-') as directory:
            root=Path(directory);root.chmod(0o700)
            auth={'auth_mode':'chatgpt','tokens':{'access_token':'fixture-access','id_token':'fixture-id',
                  'refresh_token':'fixture-refresh','account_id':'fixture-account'}}
            path=root/'managed.json';path.write_text(json.dumps(auth));path.chmod(0o600)
            calls=[]
            def runner(*args):
                calls.append(args)
                run=fake.reply_text(json.dumps({'proposals':[],'explanation':'Fixture','attention':{
                    'focus':'dialogue','ref':TARGET,'rationale':'Technical fixture','cites':[]}}))
                from dataclasses import replace
                return replace(run,stdout=run.stdout+json.dumps({'type':'iam.auth.updated','auth':auth})+'\n')
            result=verify_attention(root/'output',str(path),'iam-codex:1',runner=runner)
            self.assertEqual(result,{'status':'passed','model_calls':1,'persona_decisions':0,
                                    'core_applied':False,'shared_valid':True,'native_nonnull_wire3':True})
            self.assertEqual(len(calls),1)
            self.assertEqual(len(list((root/'output').rglob('physical/*.json'))),1)
            for artifact in (root/'output').rglob('*.json'):
                self.assertNotIn('fixture-refresh',artifact.read_text())

    def test_legacy_canonical_attention_is_not_a_native_wire3_success(self):
        with tempfile.TemporaryDirectory(prefix='iam-attention-legacy-') as directory:
            root=Path(directory);root.chmod(0o700)
            auth={'auth_mode':'chatgpt','tokens':{'access_token':'fixture-access','id_token':'fixture-id',
                  'refresh_token':'fixture-refresh','account_id':'fixture-account'}}
            path=root/'managed.json';path.write_text(json.dumps(auth));path.chmod(0o600)
            def runner(*args):
                from dataclasses import replace
                run=fake.reply([{'kind':'attend','rationale':'Legacy fixture','cites':[],
                                'body':{'focus':'dialogue','ref':TARGET}}])
                return replace(run,stdout=run.stdout+json.dumps({'type':'iam.auth.updated','auth':auth})+'\n')
            result=verify_attention(root/'output',str(path),'iam-codex:1',runner=runner)
            self.assertEqual(result['model_calls'],1)
            self.assertEqual(result['status'],'declined_or_wrong_shape')
            self.assertFalse(result['native_nonnull_wire3'])
            self.assertTrue(result['shared_valid'])
