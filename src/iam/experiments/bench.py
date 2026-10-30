"""Воспроизводимый стенд S11. Один ключевой шаг decide = один запуск общего цикла."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from iam.cycle import Cycle
from iam.domain import new_id
from iam.storage import Store

from .results import Normalizer, Results, RunResult
from .runtime import Runtime
from .scenario import Persona, Scenario


class Architecture(Protocol):
    """Фабрика свежего варианта на каждый прогон; память, мир и ядро общие по устройству."""
    name: str

    def build(self, runtime: Runtime) -> Cycle: ...


class Bench:
    MAX_DECISIONS = 4

    def __init__(self, data_root: str | Path, *, max_decisions: int = 4):
        if type(max_decisions) is not int or not 1 <= max_decisions <= 12:
            raise ValueError("предел решений: от 1 до 12")
        self.max_decisions = max_decisions
        self.results = Results(data_root)

    def run(self, scenario: Scenario | str | Path, architecture: Architecture, persona: Persona | str | Path,
            *, seed: int | None = None, memory_off: bool = False, motivation_off: bool = False) -> list[RunResult]:
        scenario = Scenario.from_dict(scenario.data) if isinstance(scenario, Scenario) else Scenario.load(scenario)
        persona = persona if isinstance(persona, Persona) else Persona.load(persona)
        seed = scenario.data.get("seed", 0) if seed is None else seed
        if type(seed) is not int:
            raise ValueError("семя должно быть целым числом")
        wanted = scenario.data.get("persona", "any")
        if wanted not in ("any", persona.id):
            raise ValueError("сценарий требует другой профиль")
        # Проверить все ветви заранее, чтобы неверный билет не оставил часть успешных прогонов.
        for run in scenario.runs:
            steps = scenario.items("step", run["id"])
            if not steps or steps[0].get("kind") != "session" or steps[0].get("action") != "start":
                raise ValueError("прогон должен начинаться с session.start")
        return [self._run(scenario, run, architecture, persona, seed, memory_off, motivation_off)
                for run in scenario.runs]

    def _run(self, scenario, run, architecture, persona, seed, memory_off, motivation_off):
        switches = {"memory_off": memory_off or run.get("switches", {}).get("memory") == "off",
                    "motivation_off": motivation_off or run.get("switches", {}).get("motivation") == "off"}
        path = self.results.reserve()
        # Только новая экспериментальная база; произвольную существующую базу этот API не принимает.
        with Store.create(path / "persona.db", new_id("exp"), created_at="2026-01-01T00:00:00.000Z") as store:
            runtime = Runtime(store, persona, seed, switches)
            steps = scenario.items("step", run["id"])
            runtime.step(steps[0])
            runtime.background(scenario.items("background", run["id"]))
            cycle = architecture.build(runtime)
            if not isinstance(cycle, Cycle) or any(getattr(cycle, attr) is not expected for attr, expected in (
                    ("ctl", runtime.controller), ("memory", runtime.memory), ("world", runtime.world),
                    ("capabilities", runtime.capabilities), ("creator", runtime.creator))):
                raise ValueError("вариант должен использовать общее ядро, память, мир и возможности стенда")
            decisions, snapshots = [], []
            status = "completed"
            for step in steps[1:]:
                if step["kind"] == "decide":
                    if len(decisions) == self.max_decisions:
                        status = "decision_limit"
                        break
                    runtime.situation = step["situation"]
                    start = store.last_seq()
                    before = len(runtime.memory.retrieved)
                    report = cycle.run(runtime.trigger(step["id"]))
                    events = runtime.controller.events_after(start)
                    decisions.append({"step": step["id"], "input": runtime.situation,
                                      "memory": [r.to_dict() for r in runtime.memory.retrieved[before:]],
                                      "motives": list(runtime.motives), "report": asdict(report),
                                      "proposals": [e.payload for e in events if e.type == "proposal.received"],
                                      "applied": [e.payload for e in events if e.changes_state],
                                      "grounds": [{"event": e.event_id, "causes": list(e.causes)} for e in events],
                                      "requests": [e.payload["request"] for e in events if e.type == "proposal.requested"]})
                else:
                    runtime.step(step)
                snapshots.append({"after": step["id"], "state": runtime.controller.state, "seq": store.last_seq()})
            if runtime.controller.session_state != "stopped":
                runtime.require(runtime.controller.stop_session())
            normalize = Normalizer()
            # Сначала журнал по seq: нормализация не зависит от сортировки случайных ключей в снимках.
            journal = normalize([e.to_dict() for e in runtime.controller.events_after(0)])
            trace = normalize({"format": 1, "scenario": scenario.data["id"], "run": run["id"],
                               "persona": asdict(persona), "architecture": architecture.name, "seed": seed,
                               "switches": switches, "status": status, "decisions": len(decisions),
                               "assertions_status": "not_evaluated", "decision_trace": decisions,
                               "snapshots": snapshots, "final_state": runtime.controller.state})
            trace["journal"] = journal
            from .conversation import records
            trace["memory_records"] = normalize([r.to_dict() for r in records(runtime.controller)])
            result = RunResult(path, trace)
            self.results.save(result)
            return result
