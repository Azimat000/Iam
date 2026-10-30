"""Редактируемые таблицы C. Числа — параметры эксперимента, не модель психологии."""
import copy
import math

from iam.cycle.attention import Focus

FEATURES = frozenset({"bias", "energy", "fatigue", "curiosity", "sociability", "persistence",
                      "has_intention", "notification", "memory_amount", "memory_valence",
                      "focused_intention", "focused_event"})
ATTENTION_RULES = {
    "activity": {"bias": 0.3, "curiosity": 0.3},
    "intention": {"bias": 0.6, "persistence": 0.25},
    "event": {"bias": 0.5, "sociability": 0.3},
    "rest": {"bias": 0.15, "fatigue": 1.2},
}
ACTION_RULES = {
    "note": {"bias": 0.15, "energy": 0.35, "curiosity": 0.3, "memory_amount": 0.15,
             "memory_valence": 0.2, "focused_intention": 0.35},
    "notice": {"bias": 0.1, "focused_event": 0.8},
    "rest": {"bias": 0.15, "fatigue": 1.2},
}


def bounded(value, default=0.5, low=0.0):
    return min(1.0, max(low, value)) if type(value) in (int, float) and math.isfinite(value) else default


def rules_copy(rules, expected):
    if not isinstance(rules, dict) or set(rules) != set(expected):
        raise ValueError("нужна таблица с полным набором кандидатов")
    for terms in rules.values():
        if not isinstance(terms, dict) or not terms or not set(terms) <= FEATURES:
            raise ValueError("нужны известные числовые признаки")
        if any(type(w) not in (int, float) or not math.isfinite(w) or abs(w) > 100 for w in terms.values()):
            raise ValueError("вес должен быть конечным числом от -100 до 100")
    return copy.deepcopy(rules)


def evaluate(rules, features, blocked):
    """Ничья разрешается по имени; недоступный кандидат виден, но победить не может."""
    candidates = {}
    for name in sorted(rules):
        terms = [{"feature": key, "value": features[key], "weight": weight,
                  "contribution": features[key] * weight} for key, weight in sorted(rules[name].items())]
        candidates[name] = {"terms": terms, "score": math.fsum(t["contribution"] for t in terms),
                            "eligible": name not in blocked, "blocked_by": blocked.get(name, [])}
    eligible = [name for name in candidates if candidates[name]["eligible"]]
    selected = max(eligible, key=lambda name: candidates[name]["score"])
    return {"selected": selected, "features": dict(features), "candidates": candidates}


def state_features(state):
    energy = bounded(state["affect"].get("energy"), 0.7)
    return {"bias": 1.0, "energy": energy, "fatigue": 1 - energy,
            **{key: bounded(state["traits"].get(key)) for key in ("curiosity", "sociability", "persistence")},
            "has_intention": float(any(isinstance(v, dict) and v.get("status") == "active"
                                      for v in state["intentions"].values())),
            "notification": 0.0, "memory_amount": 0.0, "memory_valence": 0.0,
            "focused_intention": 0.0, "focused_event": 0.0}


class FormalAttention:
    def __init__(self, rules):
        self.rules = rules

    def choose(self, trigger, state):
        features = state_features(state)
        features["notification"] = float(trigger.type == "input.arrived")
        blocked = {}
        if not features["has_intention"]:
            blocked["intention"] = ["no_active_intention"]
        if not features["notification"]:
            blocked["event"] = ["no_notification"]
        self.plan = evaluate(self.rules, features, blocked)
        chosen = self.plan["selected"]
        if chosen == "intention":
            ref = min(key for key, v in state["intentions"].items()
                      if isinstance(v, dict) and v.get("status") == "active")
            return Focus(chosen, ref, state["intentions"][ref].get("text", ""))
        return Focus(chosen, trigger.payload.get("input_id") if chosen == "event" else None)


def choose_action(rules, state, focus, records, motives, notes_available):
    features = state_features(state)
    features.update(memory_amount=min(3, len(records)) / 3,
                    memory_valence=math.fsum(bounded(r.valence, 0.0, -1.0) for r in records) / max(1, len(records)),
                    focused_intention=float(focus.focus == "intention"),
                    focused_event=float(focus.focus == "event"))
    blocked = {}
    reasons = []
    if not motives:
        reasons.append("motivation_off_or_empty")
    if not notes_available:
        reasons.append("notes_unavailable")
    if reasons:
        blocked["note"] = reasons
    if focus.focus != "event":
        blocked["notice"] = ["not_focused_on_notification"]
    return evaluate(rules, features, blocked)
