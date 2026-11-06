"""anyOf проверяет минимум одну полную альтернативу, не снимая общих ограничений."""
from pathlib import Path
import unittest
from iam.domain.jsonschema import check


class AnyOfTest(unittest.TestCase):
    def test_first_last_multiple_and_no_matching_branch(self):
        schema={'anyOf':[{'type':'integer','minimum':0},{'type':'string','pattern':'^ok$'}]}
        for value in (0,1,'ok'):
            self.assertEqual(check(schema,value,Path('.')),[])
        for value in (-1,True,'bad',None,{}):
            self.assertTrue(check(schema,value,Path('.')))
        self.assertEqual(check({'anyOf':[{},{}]},None,Path('.')),[])
        self.assertTrue(check({'anyOf':[]},None,Path('.')))
        self.assertTrue(check({'anyOf':[{'unknown_keyword':True}]},None,Path('.')))

    def test_nested_array_keeps_order_and_required_fields_and_outer_rules(self):
        item={'anyOf':[
            {'type':'object','properties':{'kind':{'const':'general'},'body':{'type':'string'}},
             'required':['kind','body'],'additionalProperties':False},
            {'type':'object','properties':{'kind':{'const':'memory'},'draft':{'type':'object',
             'properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}},
             'required':['kind','draft'],'additionalProperties':False}]}
        schema={'type':'array','minItems':1,'items':item}
        good=[{'kind':'general','body':'x'},{'kind':'memory','draft':{'text':'y'}}]
        self.assertEqual(check(schema,good,Path('.')),[])
        self.assertEqual(check(schema,list(reversed(good)),Path('.')),[])
        for bad in ([],[{'kind':'memory','body':'wrong'}],[{'kind':'memory','draft':{}}],
                    [{'kind':'general','body':'x','extra':True}]):
            self.assertTrue(check(schema,bad,Path('.')))
        self.assertTrue(check({'type':'array','minItems':3,'items':item},good,Path('.')))
