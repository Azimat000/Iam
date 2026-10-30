"""S13: внимание, состояние и выбор — правила; текст заметки — CognitiveProvider."""
import json

from iam.domain import Op
from iam.experiments.runtime import CAP_NOTES, ExperimentCycle
from iam.state import EventSpec

from .policy import HybridAttention, choose_action
from .provider import HybridProvider, INSTRUCTION, MAX_TEXT


class HybridCycle(ExperimentCycle):
    def _attend(self, rep, trigger, focus):
        applied = super()._attend(rep, trigger, focus)
        rep.stages[-1].detail["scores"] = dict(self.attention.scores)
        return applied

    def _context(self, trigger, focus, records):
        base = super()._context(trigger, focus, records)
        base = [{**c, "text": c["text"] if len(c["text"]) <= MAX_TEXT else
                 c["text"][:MAX_TEXT] + " [фрагмент обрезан]"} for c in base]
        return [{"ref": trigger.event_id, "origin": "simulation_event", "layer": "state",
                 "text": INSTRUCTION}, *base,
                {"ref": self.provider.state_ref, "origin": "observation", "layer": "state",
                 "text": json.dumps(self.provider.plan, ensure_ascii=False, sort_keys=True)}]

    def _request(self, cid, context, attempt, retry_of):
        if attempt:
            context = [*context, {**context[0], "text": "Это единственная попытка исправления. "
                                  "Проверь summarize, одно no_action, body.reason и ссылки cites."}]
        request = super()._request(cid, context, attempt, retry_of)
        request["purpose"] = "summarize"
        # Конверт ядра допускает action; HybridProvider передаёт модели только no_action.
        # Запросы общего протокола существуют и для локальных решений (usage.calls=0).
        return request

    def _decide(self, rep, trigger, focus, records):
        h = dict(self.attention.state)
        available = any(c.capability_id == CAP_NOTES and c.available for c in self.capabilities.list())
        plan = choose_action(h, focus, records, self.runtime.motives, available)
        h["decision"] = plan
        result = self._move([Op("affect", "hybrid", "set", h, True)], "динамика и оценки B",
                            EventSpec("emotion.appraised", f"emo:{rep.cycle_id}:{trigger.event_id}", {"cycle_id": rep.cycle_id}),
                            (trigger.event_id, *rep.stages[-1].events))
        self._stage(rep, "appraisal", "ok" if result.applied else "failed",
                    (e.event_id for e in result.events[:1]), {"hybrid": plan})
        if not result.applied:
            self._end(rep, "failed", "hybrid_state_rejected")
            return
        self.provider.plan = plan
        self.provider.focus = focus
        self.provider.state_ref = result.events[0].event_id
        self.provider.cites = [trigger.event_id, self.provider.state_ref, *[r.memory_id for r in records]]
        start = self.provider.calls
        super()._decide(rep, trigger, focus, records)
        self._stage(rep, "appraisal", "ok", (), {"model_calls": self.provider.calls - start,
                                                 "model_requests": self.provider.requests[start:],
                                                 "selected_by": "hybrid_rules"})
        # Эффект нагрузки/отдыха не приписываем неудавшейся операции модели.
        if rep.outcome == "done":
            delta = 0.12 if plan["selected"] == "rest" else -0.08 if rep.actions and rep.actions[0].status == "confirmed" else 0
            energy = min(1.0, max(0.0, h["energy"] + delta))
            cause = self.ctl.events_after(0)[-1].event_id
            actual_focus = self.ctl.state["affect"]["attention"]["focus"]
            settled = self._move([Op("affect", "energy", "set", energy, True),
                                 Op("affect", "hybrid.last_focus", "set", actual_focus, True),
                                 Op("affect", "hybrid.switches", "set",
                                    h["switches"] + int(actual_focus != h["last_focus"]), True)],
                                 "нагрузка/восстановление и история B",
                                 EventSpec("emotion.appraised", f"emo:{rep.cycle_id}:{cause}",
                                           {"cycle_id": rep.cycle_id}), (cause,))
            self._stage(rep, "appraisal", "ok" if settled.applied else "failed",
                        (e.event_id for e in settled.events[:1]), {"feedback": settled.status})
            if not settled.applied:
                self._end(rep, "failed", "hybrid_feedback_rejected")


class HybridArchitecture:
    name = "B-hybrid"

    def __init__(self, provider_factory):
        self.provider_factory = provider_factory

    def build(self, runtime):
        provider = self.provider_factory(runtime)
        if getattr(provider, "max_format_retries", 0) != 0:
            raise ValueError("повтор формата ведёт цикл: max_format_retries исполнителя должен быть 0")
        return HybridCycle(runtime, HybridProvider(provider), attention=HybridAttention(runtime.controller.clock.now_ms), max_rerequest=0,
                           allowed_proposals=("action", "no_action", "attend"))
