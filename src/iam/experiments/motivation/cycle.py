"""Подключение мотива к общему циклу. Мир симулируется; результат подтверждает ядро."""
import copy

from iam.cycle.attention import Focus
from iam.domain import Op, new_id
from iam.experiments.runtime import CAP_NOTES, ExperimentCycle
from iam.experiments.variants.formal import FormalProvider
from iam.experiments.variants.formal.policy import bounded
from iam.state import EventSpec

from .goals import ask_goal
from .policy import initial_interest


class MotivationAttention:
    def __init__(self, cycle):
        self.cycle = cycle

    def choose(self, trigger, state):
        self.cycle.select(trigger)
        plan = self.cycle.plan
        if plan["selected"] == "notice":
            return Focus("event", trigger.payload["input_id"])
        if plan["selected"] == "rest":
            return Focus("rest")
        if plan["intention"]:
            ref = plan["intention"]
            return Focus("intention", ref, state["intentions"][ref]["text"])
        return Focus("activity")


class MotivationProvider(FormalProvider):
    name = "motivation-formal-core"

    def propose(self, request):
        result = super().propose(request)
        if self.plan["selected"] == "note":
            result["proposals"][0]["body"]["params"] = {
                "text": "Симулируемая заметка о занятии: " + self.plan["activity"],
                "origin": "simulation_event", "activity": self.plan["activity"]}
        return result


class MotivationCycle(ExperimentCycle):
    def __init__(self, runtime, mechanism, config, goal_provider=None):
        self.mechanism = mechanism
        self.config = copy.deepcopy(config)
        self.goal_provider = goal_provider
        if mechanism.name == "goals" and goal_provider is None:
            raise ValueError("механизму goals нужен явно выбранный провайдер")
        if getattr(getattr(goal_provider, "config", None), "max_format_retries", 0) != 0:
            raise ValueError("дополнительные повторы модели запрещены")
        self.calls = 0
        super().__init__(runtime, MotivationProvider(), attention=MotivationAttention(self), max_rerequest=0,
                         allowed_proposals=("action", "no_action", "attend"))
        runtime.move([Op("preferences", "motivation", "set", {
            "interests": {name: initial_interest(**values) for name, values in config["initial"].items()},
            "need": config["need"], "experiences": {}, "goals": {}}, True),
            Op("affect", "energy", "set", config["energy"], True)], "числовые условия симуляции S15")

    @property
    def enabled(self):
        return bool(self.runtime.motives) and not self.runtime.switches["motivation_off"]

    def observe(self, event, activity, novelty, progress):
        """Явная доставка результата мира, не поиск в техническом архиве или в непрочитанных входящих."""
        if not self.enabled or event.type not in ("activity.completed", "action.confirmed", "action.failed"):
            return
        if self.ctl.find_event(event_id=event.event_id) != event:
            raise ValueError("результат не принадлежит журналу этого прогона")
        m = self.ctl.state["preferences"]["motivation"]
        old = m["interests"].get(activity, initial_interest())
        outcome = "failed" if event.type == "action.failed" else "confirmed"
        learned = self.mechanism.learn(old, outcome, novelty, progress, event.event_id)
        if learned == old:
            return
        changes = [Op("preferences", f"motivation.interests.{activity}", "set", learned, True)]
        if outcome == "confirmed":
            changes += [Op("preferences", f"motivation.experiences.{activity}", "set",
                           {"source": event.event_id}, True),
                        Op("preferences", "motivation.need", "set", max(0.0, m["need"] - 0.1), True)]
        self.runtime.move(changes, "обратная связь мотива S15", cause=event.event_id,
                          carrier=EventSpec("emotion.appraised", "emo:" + new_id("cyc") + ":" + event.event_id,
                                            {"mechanism": self.mechanism.name, "activity": activity}))

    def available(self):
        cap = self.capabilities.get(CAP_NOTES)
        return {name: CAP_NOTES for name in self.config["activities"]} if cap and cap.available else {}

    def select(self, trigger):
        m = self.ctl.state["preferences"]["motivation"]
        available = self.available()
        calls, goal_error, goal = 0, None, None
        if self.enabled and self.mechanism.name == "goals":
            goal, goal_error, calls = ask_goal(self.goal_provider, self.ctl, new_id("cyc"),
                                                m["experiences"], available)
            self.calls += calls
            if goal:
                self.runtime.move([Op("preferences", "motivation.goals." + goal["activity"], "set",
                                      {**goal, "origin": "model_explanation"}, True)],
                                  "проверенная связь предложенной цели с опытом", cause=goal["source"])
                m = self.ctl.state["preferences"]["motivation"]
            elif goal_error != "no_experience":
                pid = new_id("prop")
                self.runtime.require(self.ctl.record_event(EventSpec("proposal.rejected", f"prop:{pid}:reject",
                    {"proposal_id": pid, "kind": "intention", "reason": goal_error})))
        energy = bounded(self.ctl.state["affect"].get("energy"), 0.7)
        scores = {"rest": 0.5 + 1.2 * (1 - energy)}
        intention = next((name for name, item in sorted(self.ctl.state["intentions"].items())
                          if item.get("status") in ("active", "deferred")), None)
        intention_activity = self.config.get("intentions", {}).get(intention)
        if self.enabled:
            for activity in sorted(available):
                interest = m["interests"].get(activity, initial_interest())
                # Не более двух неудач: после этого нужно новое внешнее основание.
                if interest["failures"] < 2:
                    scores[activity] = self.mechanism.score(interest, m["need"], activity in m["goals"])
            if intention_activity in scores:
                scores[intention_activity] = max(scores[intention_activity], 2.0)
        if trigger.type == "input.arrived":
            scores["notice"] = 2.5  # только факт сообщения; срочность из его текста не читается
        selected = max(sorted(scores), key=scores.get)
        self.plan = {"selected": selected if selected in ("rest", "notice") else "note",
                     "activity": selected, "scores": scores, "energy": energy,
                     "intention": intention if selected == intention_activity else None,
                     "unfinished": intention,
                     "model_calls": calls, "goal_error": goal_error,
                     "goal_accepted": goal is not None, "mechanism": self.mechanism.name}

    def _decide(self, rep, trigger, focus, records):
        plan = self.plan
        changes = [Op("affect", "motivation_decision", "set", plan, True)]
        if plan["unfinished"] and (plan["activity"] == "notice" or plan["intention"]):
            status = "deferred" if plan["activity"] == "notice" else "active"
            if self.ctl.state["intentions"][plan["unfinished"]]["status"] != status:
                changes.append(Op("intentions", plan["unfinished"] + ".status", "transition", status, True))
        res = self._move(changes, "оценки мотива S15",
                         EventSpec("emotion.appraised", f"emo:{rep.cycle_id}:{trigger.event_id}",
                                   {"cycle_id": rep.cycle_id}), (trigger.event_id,))
        self._stage(rep, "appraisal", "ok" if res.applied else "failed",
                    (e.event_id for e in res.events[:1]), {"motivation": copy.deepcopy(plan)})
        if not res.applied:
            self._end(rep, "failed", "motivation_state_rejected")
            return
        if plan["goal_error"] in ("provider_unavailable", "request_rejected"):
            self._end(rep, "no_decision", plan["goal_error"])
            return
        self.provider.plan, self.provider.focus = plan, focus
        self.provider.cites = [trigger.event_id, res.events[0].event_id, *[r.memory_id for r in records]]
        super()._decide(rep, trigger, focus, records)
        if rep.outcome != "done":
            return
        confirmed = any(a.status == "confirmed" for a in rep.actions)
        delta = 0.12 if plan["selected"] == "rest" else -0.08 if confirmed else 0.0
        cause = self.ctl.events_after(0)[-1].event_id
        settled = self._move([Op("affect", "energy", "set", bounded(plan["energy"] + delta), True)],
                             "отдых/нагрузка S15", EventSpec("emotion.appraised", f"emo:{rep.cycle_id}:{cause}"),
                             (cause,))
        if not settled.applied:
            self._end(rep, "failed", "motivation_feedback_rejected")
            return
        # Одно подтверждённое выполнение — одна обратная связь; никакого успеха из предложения.
        for action in rep.actions:
            if action.status in ("confirmed", "failed"):
                event = self.ctl.find_event(event_id=action.events[0])
                try:
                    self.observe(event, plan["activity"], 0.0, 0.0)
                except ValueError:
                    self._end(rep, "failed", "motivation_learning_rejected")
                    return
