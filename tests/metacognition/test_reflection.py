import copy
import json

from iam.domain import new_id
from iam.intentions.completion import CompletionProvider
from iam.stubs import Reply, ScriptedProvider, act, change, rest
from state.helpers import set_
from world.helpers import LivingCase


def reflect(body=None, refs=()):
    return {"proposal_id": new_id("prop"), "kind": "reflection", "rationale": "Осмыслить опыт",
            "cites": list(refs), "body": body or {}}


class ReflectionTest(LivingCase):
    def history(self):
        return self.cycle.reflection.model.view()["history"]

    def test_optional_and_one_call_with_model_explanation(self):
        self.assertEqual(len(self.run_reply(rest()).requests), 1)
        previous = self.read()

        nested_trigger = self.due()

        def answer(req):
            self.assertEqual(req["allowed_actions"], [])
            self.assertEqual(req["allowed_proposals"], ["reflection"])
            self.assertEqual(req["purpose"], "reflect")
            self.assertEqual(self.cycle.run(nested_trigger).reason, "reflection_in_progress")
            self.assertEqual({item["layer"] for item in req["context"]}, {"state", "personal"})
            return Reply((reflect({"part": "now", "level": "hypothesis", "text": "Вероятно, чтение помогло."},
                                  [previous.memory_id]),))

        report = self.run_reply(reflect(), replies=(answer,))
        self.assertEqual(report.outcome, "done")
        self.assertEqual(len(report.requests), 2)
        saved, = self.history()
        self.assertEqual(saved["level"], "hypothesis")
        self.assertEqual(saved["origin"], "model_explanation")
        self.assertEqual(self.c.store.get_memory(saved["memory_id"]).layer, "interpretation")
        events = list(self.c.store.events())
        self.assertEqual(sum(e.type == "reflection.done" for e in events), 1)
        self.assertTrue(any(e.type == "memory.recorded" and e.payload.get("origin") == "model_explanation" for e in events))
        self.assertEqual(self.c.record_request(self.p.calls[-1]).status, "duplicate")
        forged = copy.deepcopy(self.p.calls[-1])
        forged["request_id"] = new_id("req")
        forged["idempotency_key"] = "req:" + forged["request_id"]
        self.assertEqual(self.c.record_request(forged).reason, "reflection_request")
        noncanonical = dict(self.p.calls[-1], idempotency_key=f"req:{new_id('req')}")
        self.assertEqual(self.c.record_request(noncanonical).reason, "reflection_request")

    def test_nested_direct_change_and_invalid_replies(self):
        trait = set_("traits", "traits", [{"name": "care", "value": 0.5}])
        original = self.c.state["traits"]
        result = self.c.apply(self.prop(self.c, trait, actor="model"))
        self.assertEqual(result.reason, "reflection_required")
        self.read()
        options = (Reply((reflect(),)),
                   Reply((change([trait.to_dict()]),)),
                   Reply((reflect(), reflect())),
                   Reply(error=RuntimeError("isolated")),
                   Reply((reflect({"tool_calls": []}),)),
                   Reply((reflect(),), extra={"usage": {"calls": 2, "duration_ms": 1}}),
                   Reply(raw={"bad": True}),
                   Reply(raw={"request_id": new_id("req")}),
                   Reply(()))
        for answer in options:
            report = self.run_reply(reflect(), replies=(answer,))
            self.assertEqual(report.outcome, "done")
            self.assertEqual(len(report.requests), 2)
            self.assertEqual(self.history(), [])
            self.assertEqual(self.c.state["traits"], original)
        self.assertEqual(self.run_reply(reflect({"bad": True})).outcome, "rejected")
        self.assertEqual(len(self.run_reply(reflect(), reflect(), replies=(Reply(()),)).requests), 2)

    def test_stable_change_requires_independent_spaced_unused_results(self):
        self.assertTrue(self.c.apply(self.prop(self.c, set_(
            "traits", "traits", [{"name": "care", "value": 0.5, "source": "initial"}]))).applied)
        rows = [self.read()]
        body = {"part": "now", "level": "stable_change", "text": "Устойчивый сдвиг", "trait": "care", "delta": 0.04}

        def try_change(refs, **fields):
            return self.run_reply(reflect(), replies=(Reply((reflect({**body, **fields}, refs),)),))

        try_change([rows[0].memory_id] * 3)
        self.assertFalse(self.history())
        for _ in range(2):
            self.time.advance(15_000)
            rows.append(self.write(f"Отдельный результат {len(rows)}", project=f"project{len(rows)}"))
        try_change([row.memory_id for row in rows])
        self.assertFalse(self.history())
        self.time.advance(40_000)
        rows.append(self.write("Результат после паузы", project="last_project"))
        refs = [row.memory_id for row in rows]
        for delta in (True, 0.06, 0):
            try_change(refs, delta=delta)
        try_change(refs, trait="missing")
        self.assertFalse(self.history())
        try_change(refs)
        self.assertEqual(self.c.state["traits"]["traits"][0]["value"], 0.54)
        change_entry, = self.history()
        self.assertEqual(len(change_entry["bases"]), 4)
        self.assertIn(change_entry["memory_id"], self.cycle.reflection.model.view()["parts"]["now"]["refs"])
        try_change(refs)
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(self.c.state["traits"]["traits"][0]["value"], 0.54)

    def test_provider_swap_preserves_history_and_measures_different_choices(self):
        first = self.read()
        self.run_reply(reflect(), replies=(Reply((reflect(
            {"part": "history", "level": "observation", "text": "Прочитан материал."}, [first.memory_id]),)),))
        before = self.c.state, self.c.state_version, self.history(), list(self.c.store.events())
        alternate = ScriptedProvider((Reply((rest(),)),), name="other", model="other-1")
        self.cycle.provider = CompletionProvider(alternate, self.cycle.intentions)
        self.assertEqual((self.c.state, self.c.state_version, self.history(), list(self.c.store.events())), before)
        self.assertEqual(self.cycle.run(self.due()).outcome, "done")
        self.assertEqual(self.history(), before[2])
        decisions = [(e.payload["response"]["provider"]["name"], e.payload["response"]["proposals"][0]["kind"])
                     for e in self.c.store.events() if e.type == "proposal.received"]
        self.assertEqual(decisions[0], ("scripted", "action"))
        self.assertEqual(decisions[-1], ("other", "no_action"))

    def test_inaccessible_and_non_trace_sources_do_not_count(self):
        self.read()
        hidden = new_id("mem")
        body = {"part": "history", "level": "observation", "text": "Необоснованный рассказ"}
        for refs in ([], [hidden], [self.root(self.c)]):
            self.run_reply(reflect(), replies=(Reply((reflect(body, refs),)),))
        self.assertFalse(self.history())
        self.m.off = True
        self.assertEqual(len(self.run_reply(reflect()).requests), 1)

    def test_choice_trace_new_result_forgetting_and_reopen(self):
        from iam.stubs import StubCreatorBridge
        from iam.world import READ, PersistentWorld
        from iam.world.cycle import LivingCycle, WorldMemory

        choice = act(READ, {"material_id": self.material}, rationale="Хочу увидеть пример")
        def answer(request):
            result, chosen, instructions = request["context"]
            self.assertTrue(result["ref"].startswith("mem_"))
            episode = json.loads(result["text"])
            self.assertEqual(episode["episode"], "Прочитан материал.")
            self.assertIn("Снег тает у окна", episode["experience"])
            self.assertEqual(json.loads(chosen["text"].split(". ", 1)[1]),
                             {"rationale": choice["rationale"], "cites": []})
            self.assertEqual(self.c.find_event(event_id=chosen["ref"]).payload["kind"], "action")
            self.assertEqual(instructions["origin"], "model_knowledge")
            return Reply((reflect({"part": "history", "level": "observation", "text": "Прочитан материал."},
                                  [result["ref"], chosen["ref"]]),))
        report = self.run_reply(choice, reflect(), replies=(answer,))
        entry, = self.history()
        self.assertTrue(entry["traces"])
        self.assertEqual(set(entry["refs"]), {report.actions[0].memory_id})
        reopened = self.reopen(self.c)
        other = LivingCycle(reopened, ScriptedProvider(), WorldMemory(reopened), PersistentWorld(reopened),
                            self.caps, StubCreatorBridge())
        self.assertEqual(other.reflection.model.view()["history"], [entry])
        self.assertTrue(reopened.resume_session().applied)
        self.assertEqual(reopened.record_request(self.p.calls[-1]).status, "duplicate")
        self.assertTrue(other.memory.fade(report.actions[0].memory_id, availability=0, detail=0).applied)
        self.assertFalse(other.reflection.model.view()["history"])

    def test_choice_and_new_motivation_reach_reflection_with_visible_sources(self):
        from iam.world import WRITE

        previous = self.read()
        private = self.write("Текст уже забыт", project="old_project")
        self.assertTrue(self.m.fade(private.memory_id, availability=0, detail=0).applied)
        motivation = {"proposal_id": new_id("prop"), "kind": "memory_write", "rationale": private.text,
                      "cites": [previous.memory_id], "body": {"draft": {
                          "kind": "semantic", "origin": "own_hypothesis", "content": {
                              "text": "Мне хочется продолжить наблюдение за светом",
                              "motivation": {"topic": "light", "interest": 0.8, "need": 0.2},
                          }}}}
        choice = act(WRITE, {"project_id": "light", "title": "Свет", "text": "Свет изменился."},
                     rationale="Хочу сопоставить наблюдения", cites=[previous.memory_id])

        def answer(request):
            context = {item["ref"]: item["text"] for item in request["context"]}
            ref = self.c.state["preferences"]["s22"]["light"]["memory_id"]
            self.assertEqual(json.loads(context[ref])["sources"], [previous.memory_id])
            self.assertNotIn(private.text, " ".join(context.values()))
            self.assertIn("Выбран вариант: memory_write.", context.values())
            accepted = self.c.find_event(key=f"prop:{choice['proposal_id']}:accept")
            self.assertEqual(json.loads(context[accepted.event_id].split(". ", 1)[1])["cites"], [previous.memory_id])
            return Reply((reflect({"part": "values_view", "level": "hypothesis",
                                   "text": "В наблюдении за светом мне важно сравнение."}, [ref, accepted.event_id]),))

        report = self.run_reply(motivation, choice, reflect(), replies=(answer,))
        self.assertEqual(report.actions[0].status, "confirmed")
        self.assertEqual(len(self.history()), 1)

    def test_unsolicited_request_and_raw_state_change(self):
        standalone = self.cycle._request("cyc_NoOptIn00", [], 0, None, purpose="reflect")
        self.assertEqual(self.c.record_request(standalone).reason, "reflection_opt_in")
        self.cycle.allowed_proposals += ("state_change",)
        report = self.run_reply(change([set_("selfmodel", "fake", 1).to_dict()]), rest())
        self.assertIn("reflection_required", next(s for s in report.stages if s.stage == "verification").detail["rejected"].values())
        self.assertNotIn("fake", self.c.state["selfmodel"])

    def test_previous_accessible_reflection_can_inform_later_choice(self):
        row = self.read()
        body = {"part": "now", "level": "observation", "text": "Понятнее после чтения"}
        self.run_reply(reflect(), replies=(Reply((reflect(body, [row.memory_id]),)),))
        self.run_reply(rest())
        self.assertTrue(any("Доступное прежнее объяснение" in item["text"] for item in self.p.calls[-1]["context"]))

    def test_different_editions_of_same_work_are_not_independent(self):
        self.assertTrue(self.c.apply(self.prop(self.c, set_(
            "traits", "traits", [{"name": "care", "value": 0.5}]))).applied)
        rows = []
        for edition in range(3):
            rows.append(self.write(f"Вариант {edition}", project="one_work"))
            self.time.advance(31_000)
        body = {"part": "now", "level": "stable_change", "text": "Меняюсь", "trait": "care", "delta": 0.04}
        self.run_reply(reflect(), replies=(Reply((reflect(body, [row.memory_id for row in rows]),)),))
        self.assertEqual(self.c.state["traits"]["traits"][0]["value"], 0.5)
        self.assertFalse(self.history())

    def test_late_reply_after_test_provider_replacement_cannot_change_state(self):
        from iam.stubs import StubCreatorBridge
        from iam.world import PersistentWorld
        from iam.world.cycle import LivingCycle, WorldMemory

        row = self.read()
        self.run_reply(reflect(), replies=(Reply((reflect(
            {"part": "now", "level": "observation", "text": "Замечаю интерес."}, [row.memory_id]),)),))
        choice = self.cycle._request("cyc_SwapPrompt01", self.p.calls[-2]["context"], 0, None)
        self.assertEqual(self.c.record_request(choice).status, "recorded")
        self.p.queue(Reply((rest("Решение A"),)))
        late = self.p.propose(choice)
        snapshot, history = self.c.state, self.history()
        version, epoch = self.c.state_version, self.c.epoch

        reopened = self.reopen(self.c, recover=False)
        alternate = ScriptedProvider((Reply((rest("Другое решение B"),)),), name="other", model="other-1")
        swapped = LivingCycle(reopened, alternate, WorldMemory(reopened), PersistentWorld(reopened),
                              self.caps, StubCreatorBridge())
        self.assertEqual((reopened.state, reopened.state_version, reopened.epoch), (snapshot, version, epoch))
        self.assertEqual(swapped.reflection.model.view()["history"], history)
        self.assertEqual(reopened.store.get_memory(row.memory_id), row)
        self.assertEqual(reopened.receive_response(late).reason, "deadline_unknown")
        self.assertEqual(reopened.state_version, version)

        same_context = dict(choice, request_id=new_id("req"))
        same_context["idempotency_key"] = "req:" + same_context["request_id"]
        self.assertEqual(reopened.record_request(same_context).status, "recorded")
        later = alternate.propose(same_context)
        self.assertEqual(self.p.calls[-1]["context"], alternate.calls[-1]["context"])
        self.assertNotEqual(late["proposals"][0]["body"]["reason"], later["proposals"][0]["body"]["reason"])
        self.assertEqual(reopened.receive_response(later).status, "applied")

    def test_empty_context_and_fiction_cannot_produce_biographical_observation(self):
        self.assertEqual(len(self.run_reply(reflect()).requests), 1)
        record = self.read()
        fiction = self.m.prepare(kind="semantic", origin="imagination", text="Я высадилась на вымышленной луне.",
                                 sources=[record.memory_id], layer="interpretation")
        self.assertTrue(self.m.write(fiction).applied)
        body = {"part": "history", "level": "observation", "text": "Я высадилась на луне."}
        self.run_reply(reflect(), replies=(Reply((reflect(body, [fiction.memory_id]),)),))
        rejection = [e for e in self.c.store.events() if e.type == "proposal.rejected"][-1]
        self.assertEqual(rejection.payload["reason"], "reflection_biography")
        self.assertEqual(self.history(), [])
        work = self.write("Сюжет: я высадилась на Луне.")

        def from_work(request):
            self.assertNotIn("Луне", str(request["context"]))
            episode = next(item["text"] for item in request["context"] if item["ref"] == work.memory_id)
            self.assertIn("Создана работа.", episode)
            return Reply((reflect(body, [work.memory_id]),))

        self.run_reply(reflect(), replies=(from_work,))
        rejection = [e for e in self.c.store.events() if e.type == "proposal.rejected"][-1]
        self.assertEqual(rejection.payload["reason"], "reflection_biography")
        self.assertFalse(self.history())

    def test_outside_response_and_changed_format_rejected(self):
        row = self.read()
        accepted = self.run_reply(reflect(), replies=(Reply(()),))
        # The slot remains spent when the provider response was empty.
        self.assertEqual(len(accepted.requests), 2)
        reply = reflect({"part": "now", "level": "observation", "text": "Есть след", "extra": "forged"},
                        [row.memory_id])
        self.run_reply(reflect(), replies=(Reply((reply,)),))
        rejection = [e for e in self.c.store.events() if e.type == "proposal.rejected"][-1]
        self.assertEqual(rejection.payload["reason"], "reflection_shape")
        request = self.cycle._request("cyc_Outside01", [], 0, None)
        request.update(purpose="generalize", allowed_proposals=["reflection"], allowed_actions=[])
        self.assertEqual(self.c.record_request(request).status, "recorded")
        provider = ScriptedProvider((Reply((reflect(),)),))
        result = self.c.receive_response(provider.propose(request))
        self.assertEqual(set(result.rejected.values()), {"reflection_outside_cycle"})
