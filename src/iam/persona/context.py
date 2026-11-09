"""Согласованный характер в обычном запросе выбора."""
import json


def character_context(controller, memory):
    state = controller.state
    traits = state["traits"]
    profile = traits.get("profile")
    if (controller.store.store_kind != "permanent" or not isinstance(profile, dict)
            or profile.get("format") != 1):
        return []
    birth = controller.find_event(key=f"per:{controller.persona_id}:create")
    character = {
        "kind": "character",
        "meaning": "Черты, ценности, границы и склонности описывают тебя. Выбор занятия и ответа остаётся твоим.",
        "label": profile["label"],
        "traits": [{"name": trait["name"], "value": trait["value"]} for trait in traits.get("traits", [])],
        "values": traits.get("values", []),
        "boundaries": traits.get("boundaries", []),
        "inclinations": profile["inclinations"],
    }
    if not memory._safe_item(character):
        return []
    return [{"ref": birth.event_id, "origin": "model_knowledge", "layer": "state",
             "text": json.dumps(character, ensure_ascii=False)}]
