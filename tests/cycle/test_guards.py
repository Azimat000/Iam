"""Отказы привратника посреди цикла, неверные формы ответов и заглушка-провайдер: цикл не выдумывает успех,
когда ядро не пустило очередной шаг."""
import unittest

from iam.cycle import body_violation, tool_use_signs
from iam.stubs import Reply, ScriptedProvider, act, ask, attend, rest

from .helpers import CAP_NOTES, CycleCase


def refuse(carrier_type):
    """Пользовательский валидатор привратника: не пускает переход, который несёт событие этого типа."""
    return lambda check: ["запрещено тестом"] if check.carrier_type == carrier_type else []


class GatekeeperRefusesTest(CycleCase):
    def test_attention_refused_ends_the_cycle_before_the_model(self):
        cycle = self.build(Reply((rest(),)))
        self.c.add_validator("no-attention", refuse("attention.chosen"))
        report = cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("failed", "attention_rejected"))
        self.assertEqual(report.stages[-1].status, "failed")
        self.assertEqual(self.provider.calls, [])

    def test_request_refused_means_no_call_to_the_model(self):
        cycle = self.build(Reply((rest(),)), max_output_tokens=0)   # запрос не проходит схему
        report = cycle.run(self.due())
        self.assertEqual((report.outcome, report.reason), ("no_decision", "request_bad_request"))
        self.assertEqual(self.provider.calls, [])

    def test_refused_attempt_means_the_world_is_never_asked(self):
        cycle = self.build(Reply((act(CAP_NOTES),)))
        self.c.add_validator("no-attempt", refuse("action.attempted"))
        report = cycle.run(self.due())
        self.assertEqual(report.actions[0].status, "not_attempted")
        self.assertEqual(self.world.executed, [])
        self.assertEqual(report.stages[-1].status, "failed")
        for absent in ("action.attempted", "action.confirmed", "memory.recorded"):
            self.assertNotIn(absent, self.types())

    def test_refused_outcome_leaves_the_attempt_visible_and_unremembered(self):
        cycle = self.build(Reply((act(CAP_NOTES),)))
        self.c.add_validator("no-outcome", refuse("action.confirmed"))
        report = cycle.run(self.due())
        self.assertEqual(report.actions[0].status, "attempted")
        self.assertNotIn("action.confirmed", self.types())
        self.assertNotIn("memory.recorded", self.types())
        self.assertEqual(self.world.executed[0].capability_id, CAP_NOTES)
        # после снятия запрета зависшая попытка честно закрывается как «неизвестно», а не как успех
        self.c._validators.clear()
        self.assertEqual(cycle.settle_unfinished(), [report.actions[0].action_id])
        self.assertIn("action.unknown_outcome", self.types())

    def test_unsettled_attempt_stays_visible_when_even_unknown_is_refused(self):
        cycle = self.build(Reply((act(CAP_NOTES),)))
        self.c.add_validator("no-outcome", refuse("action.confirmed"))
        report = cycle.run(self.due())
        self.c.add_validator("no-unknown", refuse("action.unknown_outcome"))
        self.assertEqual(cycle.settle_unfinished(), [])
        self.assertEqual(self.c.state["world_time"]["world"]["actions"][report.actions[0].action_id]["status"], "attempted")

    def test_refused_memory_write_is_reported(self):
        cycle = self.build(Reply((act(CAP_NOTES),)))
        self.c.add_validator("no-memory", refuse("memory.recorded"))
        report = cycle.run(self.due())
        self.assertEqual(report.actions[0].status, "confirmed")
        self.assertIsNone(report.actions[0].memory_id)
        self.assertEqual(report.stages[-1].status, "failed")

    def test_refused_creator_request_is_not_delivered(self):
        cycle = self.build(Reply((ask("k1", "помоги"),)))
        self.c.add_validator("no-crq", refuse("creator.request"))
        report = cycle.run(self.due())
        self.assertEqual(report.stages[-1].status, "failed")
        self.assertEqual(self.creator.submitted, [])


    def test_request_stuck_before_sending_may_be_proposed_again(self):
        cycle = self.build(Reply((ask("k1", "помоги"),)), Reply((ask("k1", "помоги ещё раз"),)))
        self.c.add_validator("no-crq", refuse("creator.request"))
        cycle.run(self.due())
        self.assertEqual(self.c.state["world_time"]["world"]["creator_requests"]["k1"]["status"], "accepted")
        self.c._validators.clear()
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "done")
        self.assertEqual([r.request_key for r in self.creator.submitted], ["k1"])
        self.assertEqual(self.c.state["world_time"]["world"]["creator_requests"]["k1"]["status"], "open")


class BodyFieldsTest(CycleCase):
    def test_extra_field_in_every_executable_kind_is_rejected(self):
        proposals = [act(CAP_NOTES), ask("k1", "помоги"), attend("rest"), rest("тихо")]
        for proposal in proposals:
            proposal["body"]["лишнее"] = 1
        cycle = self.build(*[Reply((p,)) for p in proposals])
        for proposal in proposals:
            report = cycle.run(self.due())
            self.assertEqual(report.outcome, "rejected", proposal["kind"])
            rej = [e for e in self.c.store.events() if e.type == "proposal.rejected"]
            self.assertEqual(rej[-1].payload["reason"], "unknown_body_field", proposal["kind"])
        self.assertEqual(self.world.executed, [])
        self.assertEqual(self.creator.submitted, [])

    def test_validator_helpers(self):
        self.assertIsNone(body_violation("no_action", {"reason": "x"}))
        self.assertIn("лишние", body_violation("no_action", {"reason": "x", "y": 1}))
        for broken in (None, "text", [], {}, {"proposals": "x"}, {"proposals": [5, "x", {"body": 3}, {"kind": "action"}]}):
            self.assertEqual(tool_use_signs(broken), [], broken)
        mixed = {"proposals": [{"proposal_id": "prop_C", "kind": "action", "body": {"params": {"Tool_Calls": 1, "stdOut": 2}}},
                               {"proposal_id": "prop_D", "kind": "message", "body": {"Executed": True}}]}
        self.assertEqual(tool_use_signs(mixed), ["prop_C: stdout", "prop_C: toolcalls", "prop_D: executed"])
        sample = {"proposals": [{"proposal_id": "prop_A", "kind": "action", "body": {"params": [{"stderr": 1}]}},
                                {"proposal_id": "prop_B", "kind": "state_change", "body": {"ops": [{"stdout": 1}], "result": 1}}]}
        self.assertEqual(tool_use_signs(sample), ["prop_A: stderr", "prop_B: result"])


class ScriptedProviderTest(CycleCase):
    def test_reply_may_be_a_function_or_a_ready_dict(self):
        provider = ScriptedProvider()
        provider.queue(lambda req: Reply((rest(),), explanation=req["purpose"]), {"raw": "dict"})
        request = {"request_id": "req_Test00001", "epoch": 1, "state_version": 3, "purpose": "choose_action"}
        first = provider.propose(request)
        self.assertEqual((first["explanation"], first["state_version"], first["provider"]["name"]),
                         ("choose_action", 3, "scripted"))
        self.assertEqual(provider.propose(request), {"raw": "dict"})
        self.assertEqual(len(provider.calls), 2)


if __name__ == "__main__":
    unittest.main()
