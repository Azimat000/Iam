"""Шесть билетов S03 на общих Runtime/StateController/Memory/World и формальном выборе C.

Отдельный раннер нужен для явной доставки размеченных результатов между циклами;
общий стенд и варианты A/B/C остаются неизменными.
"""
from dataclasses import asdict
from iam.domain import new_id
from iam.experiments.results import Normalizer, Results, RunResult
from iam.experiments.runtime import Runtime
from iam.experiments.scenario import Persona, Scenario
from iam.state import EventSpec, verify
from iam.storage import Store

from .cycle import MotivationCycle
from .fixtures import fixture


class MotivationBench:
    def __init__(self, root):
        self.results = Results(root)

    def run(self, path, mechanism, persona, *, motivation_off=False, goal_factory=None):
        scenario = path if isinstance(path, Scenario) else Scenario.load(path)
        persona = persona if isinstance(persona, Persona) else Persona.load(persona)
        config = fixture(scenario)
        return [self._run(scenario, run, mechanism, persona, config, motivation_off, goal_factory)
                for run in scenario.runs]

    def _run(self, scenario, run, mechanism, persona, config, off, goal_factory):
        steps = scenario.items("step", run["id"])
        if steps[0].get("action") != "start" or steps[0]["kind"] != "session":
            raise ValueError("прогон должен начинаться с session.start")
        path = self.results.reserve()
        switches = {"memory_off": False, "motivation_off": off or run.get("switches", {}).get("motivation") == "off"}
        with Store.create(path / "persona.db", new_id("exp"), created_at="2026-01-01T00:00:00.000Z") as store:
            runtime = Runtime(store, persona, 42, switches)
            runtime.step(steps[0])
            runtime.background(scenario.items("background", run["id"]))
            cycle = MotivationCycle(runtime, mechanism, config, goal_factory() if goal_factory else None)
            decisions, snapshots, status = [], [], "completed"
            for step in steps[1:]:
                if step["kind"] == "decide":
                    if len(decisions) == 4:
                        status = "decision_limit"
                        break
                    report = cycle.run(runtime.trigger(step["id"]))
                    decisions.append({"step": step["id"], "report": asdict(report)})
                elif step["kind"] == "model":
                    # Необоснованная свободная декларация не переводится в подтверждённую цель.
                    pid = new_id("prop")
                    runtime.require(runtime.controller.record_event(EventSpec("proposal.rejected", f"prop:{pid}:reject",
                        {"proposal_id": pid, "kind": "intention", "reason": "unstructured_unfounded_goal"})))
                else:
                    runtime.step(step)
                    if step["kind"] == "world":
                        activity, novelty, progress = config["outcomes"][step["id"]]
                        cycle.observe(runtime._trigger, activity, novelty, progress)
                snapshots.append({"after": step["id"], "state": runtime.controller.state})
            if runtime.controller.session_state != "stopped":
                runtime.require(runtime.controller.stop_session())
            if verify(store) or store.integrity_problems():
                raise ValueError("несогласованный результат эксперимента")
            norm = Normalizer()
            journal = norm([e.to_dict() for e in runtime.controller.events_after(0)])
            trace = norm({"format": 1, "scenario": scenario.data["id"], "run": run["id"],
                "persona": asdict(persona), "architecture": "C-motivation-" + mechanism.name, "seed": 42,
                "switches": switches, "status": status, "decisions": len(decisions),
                "assertions_status": "not_evaluated", "decision_trace": decisions,
                "snapshots": snapshots, "final_state": runtime.controller.state, "model_calls": cycle.calls})
            trace["journal"] = journal
            result = RunResult(path, trace)
            self.results.save(result)
            return result


def metrics(trace):
    plans = [stage["detail"]["motivation"] for decision in trace["decision_trace"]
             for stage in decision["report"]["stages"] if "motivation" in stage["detail"]]
    activities = [p["activity"] for p in plans]
    meaningful = [a for a in activities if a not in ("rest", "notice")]
    interests = trace["final_state"]["preferences"]["motivation"]["interests"]
    # Проверяется действительное происхождение по carrier/transition; не просто наличие строки evt_.
    events = {e["event_id"]: e for e in trace["journal"]}
    grounded, total = 0, 0
    for name, item in interests.items():
        for source in item["sources"]:
            total += 1
            origin = events.get(source, {})
            linked = any(source in e["causes"] and any(
                op["path"] == "motivation.interests." + name and source in op["value"]["sources"]
                for op in e["payload"].get("transition", {}).get("ops", []))
                for e in trace["journal"] if e["changes_state"])
            grounded += origin.get("type") in ("activity.completed", "action.confirmed", "action.failed") and linked
    return {"choices": ";".join(activities), "diversity": len(set(meaningful)),
            "idle_share": activities.count("rest") / max(1, len(activities)),
            "returns": sum(bool(p["intention"]) for p in plans),
            "switches": sum(a != b for a, b in zip(meaningful, meaningful[1:])),
            "grounded_sources": grounded, "total_sources": total,
            "grounding_share": grounded / total if total else None,
            "model_calls": trace["model_calls"],
            "goals_accepted": sum(p["goal_accepted"] for p in plans)}
