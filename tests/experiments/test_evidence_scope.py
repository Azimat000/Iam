"""Проверка вида свидетельства; все цели здесь — заглушечные."""
import json
import unittest
from unittest.mock import patch, PropertyMock

import experiments.test_commitment as previous
import experiments.test_development as support
from iam.experiments.commitment import CommitmentArchitecture, CommitmentCycle
from iam.experiments.development import DevelopmentRuntime
from iam.experiments.evidence_scope import INSTRUCTION, ScopeArchitecture
from iam.stubs import Reply, ScriptedProvider


class ScopeTests(unittest.TestCase):
    setUp=previous.CommitmentTests.setUp
    decide=previous.CommitmentTests.decide
    reopen=previous.CommitmentTests.reopen
    write=previous.CommitmentTests.write
    capture=previous.CommitmentTests.capture
    done=previous.CommitmentTests.done

    def open_cycle(self,calendar=0):
        self.runtime=DevelopmentRuntime(self.store,self.persona,self.switches,calendar_ms=calendar)
        self.runtime.step({'kind':'session','action':'start'})
        self.provider=ScriptedProvider();self.cycle=ScopeArchitecture(lambda _:self.provider).build(self.runtime)

    def goal(self,kind='artifact',operation='write_project',target='draft',**updates):
        body={'op':'create','id':'later','text':'Сопоставить прогноз с результатом.','operation':operation,
              'target':target,'criterion':'Есть проверяемый результат.','evidence_kind':kind}
        body.update(updates);return support.intention(body)

    def test_own_prediction_does_not_become_independent_observation(self):
        self.write();self.decide(self.goal('independent_observation'));self.reopen()
        self.write('При продолжении гипотезы получается21; это собственный расчёт.')
        q=self.capture();g=self.runtime.controller.state['intentions']['later']
        source=next(s for s in self.cycle.evidence.values() if s['operation']=='write_project')
        self.assertTrue(CommitmentCycle._completion_ok(self.cycle,g,source))
        self.assertFalse(self.cycle._completion_ok(g,source))
        signal=json.loads(q['context'][-1]['text'])['motivation'];item=signal['goal_opportunities'][0]
        self.assertTrue(item['operation_available']);self.assertFalse(item['evidence_available'])
        self.assertFalse(item['available']);self.assertEqual(item['goal_component'],0)
        self.assertFalse(any(x['accepted_goals'] for x in signal['items']))
        self.assertFalse(any(x['completion_for'] for x in json.loads(q['context'][-2]['text'])['experience_sources'].values()))

    def test_artifact_completion_is_not_semantically_verified(self):
        self.write();self.decide(self.goal());self.reopen();self.write()
        q=self.capture();self.assertEqual(q['context'][0]['text'],INSTRUCTION)
        self.assertTrue(any('artifact' in x['result_scopes'] for x in json.loads(q['context'][-2]['text'])['experience_sources'].values()))
        self.assertEqual(self.decide(self.done).outcome,'done')
        self.assertFalse(self.runtime.controller.state['intentions']['later']['semantic_verified'])

    def test_received_text_is_allowed_without_claiming_understanding(self):
        self.write();self.decide(self.goal('received_text','read_material','patterns'))
        self.decide(support.operation('read_material','patterns'))
        self.assertEqual(self.decide(lambda q:self.done(q,'Разности растут на единицу.')).outcome,'done')
        g=json.loads(self.capture()['context'][-1]['text'])['development']['intentions']['later']
        self.assertEqual(g['evidence_kind'],'received_text');self.assertFalse(g['semantic_verified'])

    def test_scope_is_from_provenance_and_does_not_create_rights(self):
        result=self.write();ref=result.actions[0].events[0]
        self.assertEqual(self.cycle.source_scopes({'operation':'open_project','base':ref}),['received_text','artifact'])
        self.assertTrue(self.cycle.can_supply({'operation':'open_project','project':'draft','evidence_kind':'artifact'}))
        projected={**self.cycle.provenance,ref:{'content_origin':'prepared_version'}}
        with patch.object(type(self.cycle),'provenance',new_callable=PropertyMock,return_value=projected):
            self.assertEqual(self.cycle.source_scopes({'operation':'open_project','base':ref}),['received_text'])
            self.assertFalse(self.cycle.can_supply({'operation':'open_project','project':'draft','evidence_kind':'artifact'}))
        for op in ('rest',None):self.assertEqual(self.cycle.source_scopes({'operation':op,'base':ref}),[])
        for op,target in (('read_material','patterns'),('open_project','missing')):
            self.assertFalse(self.cycle.can_supply({'operation':op,'project':target,'evidence_kind':'artifact'}))
        self.assertFalse(self.cycle.can_supply({'operation':'write_project','project':'draft','evidence_kind':'received_text'}))

    def test_legacy_has_no_invented_scope_and_bad_types_are_rejected(self):
        self.write()
        for kind in ('unknown',None,[]):self.assertEqual(self.decide(self.goal(kind)).outcome,'rejected')
        self.assertEqual(self.decide(self.goal(id=[])).outcome,'rejected')
        self.cycle=CommitmentArchitecture(lambda _:self.provider).build(self.runtime)
        self.decide(previous.CommitmentTests.goal(self))
        self.cycle=ScopeArchitecture(lambda _:self.provider).build(self.runtime)
        self.assertTrue(self.cycle.can_supply(self.runtime.controller.state['intentions']['later']))
        g=json.loads(self.capture()['context'][-1]['text'])['development']['intentions']['later']
        self.assertIsNone(g['evidence_kind'])
        self.write();self.assertEqual(self.decide(self.done).outcome,'done')
        self.assertEqual(self.decide(support.intention({'op':'transition','id':'later','to_status':'active'})).outcome,'rejected')

    def test_scope_immutable_and_memory_off_keeps_signals_off(self):
        self.write();self.decide(self.goal())
        self.assertEqual(self.decide(support.intention({'op':'transition','id':'later','to_status':'deferred','evidence_kind':'received_text'})).outcome,'rejected')
        self.cycle.memory.off=True
        self.assertFalse(json.loads(self.capture()['context'][-1]['text'])['motivation']['enabled'])
        self.assertEqual(self.decide(self.goal()).outcome,'rejected')
