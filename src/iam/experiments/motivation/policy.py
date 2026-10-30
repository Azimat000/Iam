"""Три экспериментальных мотива. Числа — параметры кандидатов, не законы психологии."""
from __future__ import annotations

import copy
from typing import Protocol

from iam.experiments.variants.formal.policy import bounded


def initial_interest(**overrides):
    return {"score": 0.5, "strength": 0.0, "novelty": 1.0, "skill": 0.0,
            "repeats": 0, "failures": 0, "sources": [], **overrides}


class Motivation(Protocol):
    name: str

    def score(self, interest: dict, need: float, has_goal: bool) -> float: ...

    def learn(self, interest: dict, outcome: str, novelty: float, progress: float, source: str) -> dict: ...


class NeedsMotivation:
    name = "needs"

    def score(self, interest, need, has_goal):
        return need * (0.6 + interest["score"]) + 0.3 * interest["strength"]

    def learn(self, interest, outcome, novelty, progress, source):
        result = copy.deepcopy(interest)
        # Повторная доставка результата не становится новым опытом.
        if source in result["sources"] or outcome == "unknown_outcome":
            return result
        reward = 1.0 if outcome == "confirmed" else -1.0
        result["score"] = max(-1.0, min(1.0, interest["score"] + 0.25 * (reward - interest["score"])))
        change = 0.2 * novelty + 0.2 * progress - 0.25 * (1 - novelty) if reward > 0 else -0.2
        result["strength"] = bounded(interest["strength"] + change, 0.0)
        result["novelty"] = 0.7 * interest["novelty"] + 0.3 * novelty
        result["skill"] = bounded(interest["skill"] + 0.3 * progress, 0.0)
        result["repeats"] += 1
        result["failures"] = 0 if reward > 0 else interest["failures"] + 1
        result["sources"].append(source)
        return result


class NoveltyMotivation(NeedsMotivation):
    name = "novelty"

    def score(self, interest, need, has_goal):
        return (0.6 * interest["novelty"] + 0.5 * interest["strength"] +
                0.2 * (1 - interest["skill"]) + 0.2 * interest["score"])


class GoalsMotivation(NeedsMotivation):
    name = "goals"

    def score(self, interest, need, has_goal):
        # Новая цель действует только после отдельной проверки основания и возможности.
        return super().score(interest, need, has_goal) + (0.45 if has_goal else 0.0)
