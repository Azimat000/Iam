"""Узкая операция CognitiveProvider: предложить цель по явно переданному опыту."""
import copy
import json

from iam.cycle.validator import tool_use_signs
from iam.domain import jsonschema, new_id
from iam.interfaces import ProviderError


def ask_goal(provider, controller, cycle_id, experiences, available):
    """Одно обращение; в журнале — проверенный ответ или код отказа, без сырого stderr."""
    context = [{"ref": e["source"], "origin": "simulation_event", "layer": "state",
                "text": json.dumps({"activity": name, "capability_id": available[name]}, ensure_ascii=False)}
               for name, e in sorted(experiences.items()) if name in available]
    if not context:
        return None, "no_experience", 0
    request = {"request_id": new_id("req"), "persona_id": controller.persona_id,
               "session_id": controller.session_id, "epoch": controller.epoch,
               "state_version": controller.state_version, "issued_at": controller.clock.calendar_now(),
               "life_time_ms": controller.clock.now_ms(), "purpose": "reflect",
               "context": context, "allowed_proposals": ["no_action"], "allowed_actions": [],
               "limits": {"max_output_tokens": 400, "deadline_ms": 60000, "attempt": 0},
               "response_schema": "response.schema.json", "idempotency_key": "pending",
               "cycle_id": cycle_id}
    # Инструкция — явная часть узкого контекста, не доступ к состоянию/архиву.
    request["idempotency_key"] = "req:" + request["request_id"]
    request["context"].append({"ref": context[0]["ref"], "origin": "simulation_event", "layer": "state",
                              "text": 'Верни один no_action, reason — JSON с полями activity, source, capability_id. '
                                      'Выбери только одну из переданных пар занятия и источника; cites=[source].'})
    sent = controller.record_request(request)
    if sent.event is None:
        return None, "request_rejected", 0
    error, goal = "invalid_goal", None
    try:
        response = provider.propose(copy.deepcopy(request))
    except ProviderError:
        return None, "provider_unavailable", 1
    except Exception:
        return None, "provider_unavailable", 1
    if jsonschema.errors("response", response) or tool_use_signs(response):
        return None, error, 1
    if (response["status"] != "ok" or response["tool_use_detected"] or
            any(response[k] != request[k] for k in ("request_id", "epoch", "state_version")) or
            controller.state_version != request["state_version"] or controller.epoch != request["epoch"] or
            len(response["proposals"]) != 1):
        return None, error, 1
    proposal = response["proposals"][0]
    if proposal["kind"] != "no_action" or set(proposal["body"]) != {"reason"}:
        return None, error, 1
    try:
        goal = json.loads(proposal["body"]["reason"])
    except (ValueError, TypeError):
        return None, error, 1
    if not isinstance(goal, dict) or set(goal) != {"activity", "source", "capability_id"}:
        return None, error, 1
    if not all(isinstance(v, str) for v in goal.values()):
        return None, error, 1
    activity = goal["activity"]
    if (activity not in available or goal["capability_id"] != available[activity] or
            activity not in experiences or goal["source"] != experiences[activity]["source"] or
            proposal["cites"] != [goal["source"]]):
        return None, error, 1
    return goal, None, 1


class ScriptedGoals:
    """Заглушка операции модели; не выдаётся за живой Codex или понимание текста."""
    def propose(self, request):
        item = request["context"][0]
        goal = {**json.loads(item["text"]), "source": item["ref"]}
        return {"request_id": request["request_id"], "epoch": request["epoch"],
                "state_version": request["state_version"], "provider": {"name": "scripted", "model": "fixture"},
                "status": "ok", "tool_use_detected": False, "usage": {"calls": 1, "duration_ms": 0},
                "explanation": "scripted goal, not a live model",
                "proposals": [{"proposal_id": new_id("prop"), "kind": "no_action", "rationale": "fixture",
                               "cites": [item["ref"]], "body": {"reason": json.dumps(goal)}}]}
