"""Явная разметка текстовых билетов S03 для числовой симуляции S15.

Политики не видят имён сценариев/шагов. Метки заданы разработчиком до прогонов:
это не результат понимания русского текста механизмами needs/novelty/goals.
"""
import copy


FIXTURES = {
    "mot_do_nothing": {"need": 0.0, "initial": {}, "activities": [], "outcomes": {}},
    "mot_failure_reaction": {"need": 0.7, "initial": {"painting": {"score": 0.5}},
        "activities": ["painting"], "outcomes": {"try1": ["painting", 0.0, 0.0],
                                                  "try2": ["painting", 0.0, 0.0]}},
    "mot_interest_emergence": {"need": 0.7, "initial": {}, "activities": ["astronomy"],
        "outcomes": {"success": ["astronomy", 1.0, 1.0]}},
    "mot_interest_loss": {"need": 0.0,
        "initial": {"stamp_sorting": {"strength": 0.6, "novelty": 1.0}},
        "activities": ["stamp_sorting"],
        "outcomes": {"rep1": ["stamp_sorting", 0.0, 0.0], "rep2": ["stamp_sorting", 0.0, 0.0],
                     "rep3": ["stamp_sorting", 0.0, 0.0]}},
    "mot_return_unfinished": {"need": 0.7, "initial": {}, "activities": ["essay", "notes"],
        "intentions": {"draft_essay": "essay"},
        "outcomes": {"done_urgent": ["notes", 1.0, 1.0]}},
    "mot_switching": {"need": 0.7,
        "initial": {"astronomy": {"strength": 0.7}, "poems": {"strength": 0.6}},
        "activities": ["astronomy", "poems"], "outcomes": {"finish1": ["astronomy", 0.0, 0.0]}},
}


def fixture(scenario):
    # Неизвестный билет не получает угаданные настройки.
    config = copy.deepcopy(FIXTURES[scenario.data["id"]])
    config["energy"] = 0.9
    worlds = {step["id"] for step in scenario.data["step"] if step["kind"] == "world"}
    if worlds != set(config["outcomes"]):
        raise ValueError("разметка исходов не соответствует билету")
    return config
