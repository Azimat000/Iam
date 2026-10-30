"""S14: локальный формальный выбор C на неизменном общем стенде S11."""
import copy
import json

from iam.domain import Op, new_id
from iam.experiments.runtime import CAP_NOTES, ExperimentCycle
from iam.state import EventSpec

from .language import TemplateLanguage
from .policy import ACTION_RULES, ATTENTION_RULES, FormalAttention, choose_action, rules_copy


class FormalProvider:
    """Адаптер локального решения к проверяемому конверту ядра; не языковая модель."""
    name = "formal-core"

    def __init__(self):
        self.language = TemplateLanguage()

    def propose(self, request):
        selected = self.plan["selected"]
        text = self.language.render(selected)
        rationale = "Правила C: " + json.dumps(self.plan, ensure_ascii=False, sort_keys=True)
        if selected == "note":
            kind = "action"
            body = {"capability_id": CAP_NOTES, "params": {"text": text, "origin": "simulation_event"}}
        elif selected == "notice":
            kind, body = "attend", {"focus": self.focus.focus, "ref": self.focus.ref}
        else:
            kind, body = "no_action", {"reason": text}
        return {"request_id": request["request_id"], "epoch": request["epoch"],
                "state_version": request["state_version"], "provider": {"name": self.name, "model": "utility-1"},
                "status": "ok", "tool_use_detected": False, "usage": {"calls": 0, "duration_ms": 0},
                "explanation": rationale, "proposals": [{"proposal_id": new_id("prop"), "kind": kind,
                "rationale": rationale, "cites": self.cites, "body": body}]}


class FormalCycle(ExperimentCycle):
    def __init__(self, runtime, attention_rules, action_rules):
        self.action_rules = action_rules
        super().__init__(runtime, FormalProvider(), attention=FormalAttention(attention_rules), max_rerequest=0,
                         allowed_proposals=("action", "no_action", "attend"))

    def _attend(self, rep, trigger, focus):
        applied = super()._attend(rep, trigger, focus)
        rep.stages[-1].detail["formal_attention"] = copy.deepcopy(self.attention.plan)
        return applied

    def _decide(self, rep, trigger, focus, records):
        available = any(c.capability_id == CAP_NOTES and c.available for c in self.capabilities.list())
        plan = choose_action(self.action_rules, self.ctl.state, focus, records, self.runtime.motives, available)
        audit = {"attention": self.attention.plan, "action": plan}
        result = self._move([Op("affect", "formal", "set", audit, True)], "таблицы полезности C",
                            EventSpec("emotion.appraised", f"emo:{rep.cycle_id}:{trigger.event_id}",
                                      {"cycle_id": rep.cycle_id}), (trigger.event_id, *rep.stages[-1].events))
        self._stage(rep, "appraisal", "ok" if result.applied else "failed",
                    (e.event_id for e in result.events[:1]), {"formal": audit, "model_calls": 0,
                                                           "selected_by": "formal_rules"})
        if not result.applied:
            self._end(rep, "failed", "formal_state_rejected")
            return
        self.provider.plan, self.provider.focus = plan, focus
        self.provider.cites = [trigger.event_id, result.events[0].event_id, *[r.memory_id for r in records]]
        super()._decide(rep, trigger, focus, records)
        if rep.outcome == "done":
            confirmed = any(a.status == "confirmed" for a in rep.actions)
            delta = 0.12 if plan["selected"] == "rest" else -0.08 if confirmed else 0.0
            energy = min(1.0, max(0.0, plan["features"]["energy"] + delta))
            cause = self.ctl.events_after(0)[-1].event_id
            settled = self._move([Op("affect", "energy", "set", energy, True)], "нагрузка/отдых C",
                                 EventSpec("emotion.appraised", f"emo:{rep.cycle_id}:{cause}",
                                           {"cycle_id": rep.cycle_id}), (cause,))
            self._stage(rep, "appraisal", "ok" if settled.applied else "failed",
                        (e.event_id for e in settled.events[:1]), {"feedback": settled.status})
            if not settled.applied:
                self._end(rep, "failed", "formal_feedback_rejected")


class FormalArchitecture:
    name = "C-formal"

    def __init__(self, *, attention_rules=None, action_rules=None):
        self.attention_rules = rules_copy(ATTENTION_RULES if attention_rules is None else attention_rules, ATTENTION_RULES)
        self.action_rules = rules_copy(ACTION_RULES if action_rules is None else action_rules, ACTION_RULES)

    def build(self, runtime):
        return FormalCycle(runtime, copy.deepcopy(self.attention_rules), copy.deepcopy(self.action_rules))
