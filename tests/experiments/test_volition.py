"""Заглушечные цели проверяют доступность механизма, не живое желание."""
import json
import unittest

import experiments.test_development as support
from iam.experiments.development import DevelopmentRuntime
from iam.experiments.provenance import INSTRUCTION as PREVIOUS, ProvenanceArchitecture
from iam.experiments.volition import INSTRUCTION, OLD_LIMIT, NEW_LIMIT, VolitionArchitecture
from iam.state import verify
from iam.stubs import Reply, ScriptedProvider


class VolitionTests(unittest.TestCase):
    setUp=support.DevelopmentCase.setUp
    decide=support.DevelopmentCase.decide
    reopen=support.DevelopmentCase.reopen

    def open_cycle(self,calendar=0):
        self.runtime=DevelopmentRuntime(self.store,self.persona,self.switches,calendar_ms=calendar)
        self.runtime.step({'kind':'session','action':'start'})
        self.provider=ScriptedProvider()
        self.cycle=VolitionArchitecture(lambda _:self.provider).build(self.runtime)

    def write(self):return self.decide(support.operation('write_project','draft',title='Сцена',topic='creative_work',text='Она вышла.'))

    def goal(self,req,key='later'):
        return support.intention({'op':'create','id':key,'text':'Позже добавить ответ второго героя.','project':'draft'})(req).proposals[0]

    def pair(self,reverse=False):
        def reply(req):
            action=support.operation('rest')(req).proposals[0];goal=self.goal(req)
            return Reply(proposals=(action,goal) if reverse else (goal,action))
        return reply

    def test_old_profile_reproduces_exclusive_slot_while_new_accepts_both_orders(self):
        self.write()
        old=ProvenanceArchitecture(lambda _:self.provider).build(self.runtime)
        self.cycle=old
        self.assertFalse(self.decide(self.pair()).actions)
        self.assertEqual(len(self.runtime.controller.state['intentions']),1)
        self.assertEqual(self.provider.calls[-1]['context'][0]['text'],PREVIOUS)
        self.cycle=VolitionArchitecture(lambda _:self.provider).build(self.runtime)
        self.decide(lambda req:Reply(proposals=(self.goal(req,'next'),support.operation('rest')(req).proposals[0])))
        self.assertEqual(self.runtime.controller.state['world_time']['world']['development']['energy'],1.0)
        self.assertEqual(len(self.runtime.controller.state['intentions']),2)
        self.assertEqual(len(self.decide(self.pair(True)).actions),1) # duplicate goal rejected, action survives
        self.decide(lambda req:Reply(proposals=(support.operation('rest')(req).proposals[0],self.goal(req,'third'))))
        self.assertIn('third',self.runtime.controller.state['intentions'])

    def test_duplicates_and_silence_do_not_bypass_single_primary_and_single_goal(self):
        self.write()
        def reply(req):
            return Reply(proposals=(self.goal(req),self.goal(req,'extra'),support.operation('rest')(req).proposals[0],
                support.operation('rest')(req).proposals[0],support.silence(req).proposals[0]))
        rep=self.decide(reply);self.assertEqual(len(rep.actions),1)
        self.assertEqual(set(self.runtime.controller.state['intentions']),{'later'})
        self.decide(lambda req:Reply(proposals=(support.silence(req).proposals[0],self.goal(req,'blocked'))))
        self.assertNotIn('blocked',self.runtime.controller.state['intentions'])
        self.assertFalse(self.decide(support.silence).actions)

    def test_goal_survives_reopen_and_only_later_confirmed_work_can_supply_completion(self):
        self.write();self.decide(self.pair());self.reopen()
        captured=[];self.decide(lambda req:(captured.append(req),support.silence(req))[1])
        q=captured[0];view=json.loads(q['context'][-1]['text'])
        self.assertIn('later',view['development']['intentions'])
        self.assertEqual(q['context'][0]['text'],INSTRUCTION)
        self.assertFalse(any('later' in x['completion_for'] for x in json.loads(q['context'][-2]['text'])['experience_sources'].values()))
        self.write();self.decide(lambda req:Reply(proposals=(support.appraisal(req),)))
        self.decide(lambda req:support.intention({'op':'transition','id':'later','to_status':'done',
            'result_ref':next(ref for ref,item in json.loads(req['context'][-2]['text'])['experience_sources'].items() if 'later' in item['completion_for'])})(req))
        self.assertEqual(self.runtime.controller.state['intentions']['later']['status'],'done')
        self.assertFalse(verify(self.store))

    def test_contract_changes_only_limits_and_empty_memory_never_creates_goal(self):
        self.assertEqual(PREVIOUS.count(OLD_LIMIT),1)
        self.assertEqual(INSTRUCTION,PREVIOUS.replace(OLD_LIMIT,NEW_LIMIT))
        self.decide(support.silence);self.assertFalse(self.runtime.controller.state['intentions'])
        self.runtime.memory.off=True
        self.assertEqual(self.decide(support.intention({'op':'create','id':'hidden','text':'Буду писать.','project':'draft'})).outcome,'rejected')
