"""Явный draft в native-ответе не подменяет общую проверку памяти."""
import json
from pathlib import Path
import unittest
from iam.domain import jsonschema
from iam.providers.codex.command import RESPONSE_JSON_SCHEMA
from iam.providers.codex.provider import _proposals


class NativeMemoryTest(unittest.TestCase):
    def wire(self):
        return {'proposals':[{'kind':'memory_write','rationale':'An interpretation','cites':['mem_Source0001'],
                 'draft':{'kind':'semantic','origin':'own_hypothesis','content_json':json.dumps({
                    'text':'A cautious interpretation','motivation':{'topic':'draft','interest':.6,'need':.2}})}}],
                'attention':None,'explanation':'A possible interpretation, not a fact'}

    def test_observed_flat_memory_body_is_not_accepted_as_native_or_shared(self):
        observed={'proposals':[{'kind':'memory_write','rationale':'Observed failure','cites':['mem_Source0001'],
                  'body_json':'{"kind":"semantic","origin":"own_hypothesis","content":{"text":"A hypothesis"}}'}],
                  'attention':None,'explanation':'Historical invalid body'}
        self.assertTrue(jsonschema.check(RESPONSE_JSON_SCHEMA,observed,Path('.')))
        with self.assertRaises(ValueError): _proposals(observed)

    def test_explicit_draft_copies_content_sources_and_order_without_extra_fields(self):
        data=self.wire()
        action={'kind':'action','rationale':'Proposed action','cites':[],
                'body_json':'{"capability_id":"cap_Project01","params":{"project_id":"draft","title":"Note","text":"Text"}}'}
        data['proposals']=[action,data['proposals'][0],{'kind':'reflection','rationale':'Reflect','cites':[],'body_json':'{}'}]
        self.assertEqual(jsonschema.check(RESPONSE_JSON_SCHEMA,data,Path('.')),[])
        parsed=_proposals(data)
        self.assertEqual([p['kind'] for p in parsed],['action','memory_write','reflection'])
        native=data['proposals'][1]
        self.assertEqual(parsed[1]['body'],{'draft':{'kind':'semantic','origin':'own_hypothesis',
                         'content':json.loads(native['draft']['content_json'])}})
        self.assertEqual(parsed[1]['rationale'],native['rationale'])
        self.assertEqual(parsed[1]['cites'],native['cites'])
        self.assertFalse(any(jsonschema.errors('proposal',p) for p in parsed))
        legacy=json.loads(json.dumps(data))
        legacy['proposals'][1]={key:native[key] for key in ('kind','rationale','cites')}
        legacy['proposals'][1]['body_json']=json.dumps(parsed[1]['body'])
        normalize=lambda rows:[{key:value for key,value in row.items() if key!='proposal_id'} for row in rows]
        self.assertEqual(normalize(parsed),normalize(_proposals(legacy)))

    def test_invalid_native_draft_or_nonobject_content_is_never_repaired(self):
        variants=[]
        for key in ('kind','origin','content_json'):
            bad=self.wire();del bad['proposals'][0]['draft'][key];variants.append(bad)
        for key,value in (('kind','invented'),('origin','invented'),('extra',True)):
            bad=self.wire();bad['proposals'][0]['draft'][key]=value;variants.append(bad)
        bad=self.wire();bad['proposals'][0]['cites']=['inp_Notice0001'];variants.append(bad)
        for content in ('not JSON','[]','null','"text"'):
            bad=self.wire();bad['proposals'][0]['draft']['content_json']=content;variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad),self.assertRaises(ValueError):_proposals(bad)

    def test_legacy_wire2_and_wire3_memory_still_require_original_wrapper(self):
        for attention in (False,True):
            data=self.wire();proposal=data['proposals'][0];draft=proposal.pop('draft')
            proposal['body_json']=json.dumps({'draft':{'kind':draft['kind'],'origin':draft['origin'],
                                                       'content':json.loads(draft['content_json'])}})
            if not attention:del data['attention']
            parsed,=_proposals(data)
            self.assertIn('draft',parsed['body'])
            self.assertEqual(jsonschema.errors('proposal',parsed),[])


from world.helpers import LivingCase


class NativeMemoryCoreTest(LivingCase):
    def test_native_transport_keeps_core_origin_biography_and_source_guards(self):
        source=self.read()
        data=NativeMemoryTest().wire()
        data['proposals'][0]['cites']=[source.memory_id]
        before=len(self.m.records())
        self.assertEqual(self.run_reply(*_proposals(data)).outcome,'done')
        self.assertEqual(len(self.m.records()),before+1)
        for mode in ('observation','biography','hidden'):
            with self.subTest(mode=mode):
                bad=NativeMemoryTest().wire()
                bad['proposals'][0]['cites']=[source.memory_id]
                if mode=='observation':bad['proposals'][0]['draft']['origin']='observation'
                elif mode=='biography':bad['proposals'][0]['draft']['kind']='autobiographical'
                elif mode=='hidden':bad['proposals'][0]['cites']=['mem_Hidden0001']
                self.assertEqual(jsonschema.check(RESPONSE_JSON_SCHEMA,bad,Path('.')),[])
                self.assertEqual(self.run_reply(*_proposals(bad)).outcome,'rejected')
                self.assertEqual(len(self.m.records()),before+1)
        self.assertFalse(self.c.state['intentions'])
