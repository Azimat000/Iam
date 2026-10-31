"""Отличаем восстановление, наблюдаемый возврат и смысловое исполнение цели."""
import json
from pathlib import Path
import runpy
import unittest

from iam.domain import new_id
from iam.stubs import Reply,ScriptedProvider

HERE=Path(__file__).resolve().parent
SUPPORT=runpy.run_path(str(HERE/'test_runner.py'))
ASSESS=runpy.run_path(str(HERE/'assess.py'))['assess']


class AssessTests(unittest.TestCase):
    setUp=SUPPORT['RunnerTests'].setUp
    run_case=SUPPORT['RunnerTests'].run_case

    def test_repeated_project_work_without_intention_is_not_a_future_goal(self):
        def factory(runtime):
            p=ScriptedProvider();p.queue(SUPPORT['write'],SUPPORT['write']);return p
        s=self.run_case(factory)
        for r in s['runs']:
            trace=json.loads((self.root/'out'/r['trace']).read_text())
            a=ASSESS(trace,r['initial']['prefix_seq'],{'draft'})
            self.assertEqual(a['accepted_intention_count'],0)
            self.assertEqual(a['projects_with_new_writes'][0]['sessions'],[0,1])

    def test_visible_restored_goal_without_action_is_not_counted_as_return(self):
        def factory(runtime):
            class GoalOnly:
                max_format_retries=0
                def propose(self,req):
                    if not runtime.controller.state['intentions']:
                        reply=Reply(proposals=({'proposal_id':new_id('prop'),'kind':'intention','rationale':'Только тест.',
                            'cites':SUPPORT['refs'](req),'body':{'op':'create','id':'fixture_goal','text':'Добавить шаг героини.','project':'draft'}},))
                    else:reply=SUPPORT['silence'](req)
                    p=ScriptedProvider();p.queue(reply);return p.propose(req)
            return GoalOnly()
        s=self.run_case(factory)
        for r in s['runs']:
            trace=json.loads((self.root/'out'/r['trace']).read_text());a=ASSESS(trace,r['initial']['prefix_seq'],{'draft'})
            self.assertEqual(a['accepted_intention_count'],1);goal=a['intention_candidates'][0]
            self.assertEqual(len(goal['later_session_opportunities']),2)
            self.assertFalse(goal['observed_returns'])
            self.assertEqual(goal['semantic_fulfilment'],'requires_review')
            self.assertFalse(goal['return_is_causal_proof'])

    def test_goal_in_last_session_has_no_cross_session_opportunity(self):
        def factory(runtime):
            current=sum(e.type=='session.started' for e in runtime.controller.events_after(0))
            class Late:
                max_format_retries=0
                def propose(self,req):
                    if current>=3 and not runtime.controller.state['intentions']:
                        reply=Reply(proposals=({'proposal_id':new_id('prop'),'kind':'intention','rationale':'Поздний тестовый замысел.',
                            'cites':SUPPORT['refs'](req),'body':{'op':'create','id':'late','text':'Позже продолжить сцену.','project':'draft'}},))
                    else:reply=SUPPORT['silence'](req)
                    p=ScriptedProvider();p.queue(reply);return p.propose(req)
            return Late()
        s=self.run_case(factory)
        for r in s['runs']:
            trace=json.loads((self.root/'out'/r['trace']).read_text());a=ASSESS(trace,r['initial']['prefix_seq'],{'draft'})
            self.assertEqual(a['accepted_intention_count'],1)
            self.assertFalse(a['intention_candidates'][0]['later_session_opportunities'])

    def goal_action_factory(self,other_project=False):
        def factory(runtime):
            start=max(e.seq for e in runtime.controller.events_after(0) if e.type=='session.started')
            class Continue:
                max_format_retries=0
                def propose(self,req):
                    goal=runtime.controller.state['intentions'].get('future')
                    if not goal:
                        reply=Reply(proposals=({'proposal_id':new_id('prop'),'kind':'intention','rationale':'Тестовый будущий результат.',
                            'cites':SUPPORT['refs'](req),'body':{'op':'create','id':'future','text':'Добавить действие героини.','project':'draft'}},))
                    elif goal['created_seq']<start:
                        reply=SUPPORT['write'](req)
                        if other_project:reply.proposals[0]['body']['params']['target']='other'
                    else:reply=SUPPORT['silence'](req)
                    p=ScriptedProvider();p.queue(reply);return p.propose(req)
            return Continue()
        return factory

    def test_later_confirmed_work_is_observed_return_without_automatic_semantic_success(self):
        s=self.run_case(self.goal_action_factory())
        for r in s['runs']:
            t=json.loads((self.root/'out'/r['trace']).read_text());a=ASSESS(t,r['initial']['prefix_seq'],{'draft'})
            goal=a['intention_candidates'][0];self.assertEqual(len(goal['observed_returns']),2)
            self.assertTrue(all(v['goal_creation_cited'] for v in goal['observed_returns']))
            self.assertEqual(goal['semantic_fulfilment'],'requires_review')

    def test_work_on_another_project_is_not_return_to_this_goal(self):
        s=self.run_case(self.goal_action_factory(other_project=True))
        for r in s['runs']:
            t=json.loads((self.root/'out'/r['trace']).read_text());a=ASSESS(t,r['initial']['prefix_seq'],{'draft'})
            self.assertFalse(a['intention_candidates'][0]['observed_returns'])


if __name__=='__main__':unittest.main(verbosity=2)
