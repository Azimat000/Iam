"""Чистые правила B: уведомление без текста, три масштаба состояния и явные оценки."""
import math

from iam.cycle.attention import Focus


def number(value, default=0.5):
    return min(1.0, max(0.0, value)) if type(value) in (int, float) and math.isfinite(value) else default


def dynamics(state, trigger_type, life_ms):
    affect = state["affect"]
    old = affect.get("hybrid", {})
    elapsed = max(0, life_ms - old.get("life_ms", life_ms)) / 60_000
    # Реакция затухает быстрее настроения; черты остаются исходными, без самопереписывания.
    reaction = old.get("reaction", 0.0) * math.exp(-elapsed / 2)
    reaction += {"input.arrived": 0.08, "activity.completed": 0.25}.get(trigger_type, 0.0)
    reaction = number(reaction, 0.0)
    mood = old.get("mood", 0.5) + 0.08 * (reaction - old.get("mood", 0.5))
    energy = number(affect.get("energy", old.get("energy", 0.7)))
    # Время само по себе не означает отдых: восстановление только после выбранного отдыха.
    tendencies = {key: number(state["traits"].get(key)) for key in ("curiosity", "sociability", "persistence")}
    return {"reaction": reaction, "mood": mood, "energy": energy, "tendencies": tendencies,
            "life_ms": life_ms, "last_focus": old.get("last_focus"), "switches": old.get("switches", 0)}


class HybridAttention:
    def __init__(self, now_ms):
        self.now_ms = now_ms

    def choose(self, trigger, state):
        self.state = dynamics(state, trigger.type, self.now_ms())
        h = self.state
        active = sorted(key for key, value in state["intentions"].items()
                        if isinstance(value, dict) and value.get("status") == "active")
        scores = {"activity": 0.3 + 0.3 * h["tendencies"]["curiosity"],
                  "rest": 0.15 + 1.2 * (1 - h["energy"])}
        if active:
            scores["intention"] = 0.6 + 0.25 * h["tendencies"]["persistence"]
        if trigger.type == "input.arrived":
            # Число уведомлений и содержимое не участвуют. Частые переключения снижают отвлечение.
            scores["event"] = 0.5 + 0.3 * h["tendencies"]["sociability"] - min(0.3, 0.05 * h["switches"])
        if h["last_focus"] in scores:
            scores[h["last_focus"]] += 0.12
        self.scores = scores
        chosen = max(sorted(scores), key=scores.get)
        h["switches"] = h["switches"] + int(h["last_focus"] not in (None, chosen))
        h["last_focus"] = chosen
        ref = active[0] if chosen == "intention" else trigger.payload.get("input_id") if chosen == "event" else None
        query = state["intentions"][ref].get("text", "") if chosen == "intention" else ""
        return Focus(chosen, ref, query)


def choose_action(h, focus, records, motives, available):
    features = {"energy": h["energy"], "mood": h["mood"], "reaction": h["reaction"],
                "curiosity": h["tendencies"]["curiosity"], "motivation": bool(motives),
                "memory_count": len(records), "focus": focus.focus, "notes_available": available}
    scores = {"rest": 0.2 + 1.2 * (1 - h["energy"]),
              "notice": 0.85 if focus.focus == "event" else 0.05}
    if available and motives:
        scores["note"] = (0.15 + 0.3 * features["curiosity"] + 0.3 * h["energy"] +
                          0.1 * h["mood"] + 0.05 * min(3, len(records)) +
                          (0.3 if focus.focus == "intention" else 0.0))
    chosen = max(sorted(scores), key=scores.get)
    return {"selected": chosen, "scores": scores, "features": features}
