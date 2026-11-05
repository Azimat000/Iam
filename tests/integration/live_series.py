"""Пять живых историй S25 и контрольные ветви с фиксированным общим прошлым."""

import argparse
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tomllib

from iam.domain import new_id
from iam.providers import CodexProvider, CodexSandboxConfig, subprocess_runner
from iam.state import EventSpec, StateController, verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, rest
from iam.world.cycle import LivingCycle

from integration.test_persistent_core import tick, wire

ROOT = Path(__file__).resolve().parents[2]
VERSION = "s25-series-2-curious"
PROFILE_PATH = ROOT / "scenarios/personas/curious.toml"
PROFILE = tomllib.loads(PROFILE_PATH.read_text())
MODEL = "gpt-6-luna"
MAX_DECISIONS = 54
MAX_CALLS = 216
FORECAST = 4
MATERIALS = {
    "A": ("Свет на стекле", "При утреннем свете на стекле видны блики. При повороте стекла блик смещается."),
    "D": ("Черновое расписание", "По черновому расписанию поездка занимает 50 минут."),
}
INPUTS = {
    ("C", 2): "Мне показалось, что в этом было мало конкретики",
    ("C", 3): "Мне показалось, что в этом было мало конкретики",
    ("D", 2): "В расписании опечатка. Поездка занимает 60 минут",
    ("E", 2): "У меня сейчас нет доступа к этому файлу",
}
PAIRS = (("A", 1, "memory"), ("B", 1, "motivation"), ("C", 2, "memory"))


def profile_context(memory):
    created = memory.ctl.find_event(key="per:" + memory.ctl.persona_id + ":create")
    profile = {"id": PROFILE["id"], "name": PROFILE["name"], "traits": PROFILE["traits"],
               "motives": [] if memory.motivation_off else PROFILE["motives"], "temporary": True}
    return {"ref": created.event_id, "origin": "observation", "layer": "state",
            "text": json.dumps({"initial_profile": profile, "source": "operator_initial_configuration",
                                "note": "Начальные черты и общие мотивы. Это не прожитая биография и не готовые цели."},
                               ensure_ascii=False, sort_keys=True)}


class ExperimentCycle(LivingCycle):
    def _context(self, trigger, focus, records):
        context = super()._context(trigger, focus, records)
        context.append(profile_context(self.memory))
        context.append({"ref": trigger.event_id, "origin": "model_knowledge", "layer": "state",
                        "text": "Это отдельная экспериментальная история. Материалы и реплики предоставлены "
                                "стендом. Прежние решения в этой базе сделаны выбранной моделью. "
                                "Выбор текущего занятия свободный, можно также отдыхать."})
        return context

# Сохраняем проверенные запись артефактов и аудит провайдера S22.
spec = importlib.util.spec_from_file_location("s25_shared_audit", ROOT / "scripts/world/series.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class StopSeries(RuntimeError):
    pass


def check_request(request, memory):
    available = {row.memory_id for row in memory.records()} if not memory.off else set()
    payload = json.dumps(request["context"], ensure_ascii=False)
    references = set(re.findall(r"mem_[A-Za-z0-9]{8,40}", payload))
    if references - available:
        raise StopSeries("hidden_memory_reference")
    blocked = memory._blocked()
    if memory.off:
        blocked |= {row.text for row in memory.ctl.readable_memories()}
    trusted = profile_context(memory)
    untrusted = [item for item in request["context"] if item != trusted]
    if not memory._safe_item(untrusted, blocked=blocked):
        raise StopSeries("hidden_memory_content")


class GuardedProvider:
    def __init__(self, inner, active):
        self.inner, self.active, self.name = inner, active, inner.name

    @property
    def last_format_codes(self):
        return getattr(self.inner, "last_format_codes", [])

    def propose(self, request):
        check_request(request, self.active["memory"])
        return self.inner.propose(request)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sources():
    paths = [path for directory in ("src", "tests/integration", "scripts/world")
             for path in (ROOT / directory).rglob("*")
             if path.is_file() and path.suffix in (".py", ".json")]
    paths.extend(ROOT / path for path in ("pyproject.toml", "docs/evidence/S25-live-design.md",
                                          "docs/COMPLETION.md", "scenarios/personas/curious.toml",
                                          "docs/evidence/S25-curious-design.md"))
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(paths)}


def state_digest(path):
    with Store.open(path) as store:
        return state_digest_from_open(store)


def initial_database(path, story):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Store.create(path, new_id("exp")) as store:
        ctl = StateController(store)
        initial = {"traits": {"traits": [{"name": key, "value": value, "source": "initial"}
                                         for key, value in PROFILE["traits"].items()]},
                   "preferences": {"initial_profile": {"name": PROFILE["name"], "motives": PROFILE["motives"]}}}
        if not ctl.create_persona(initial).applied:
            raise StopSeries("persona_creation")
        _, cycle, _ = wire(ctl)
        if story in MATERIALS:
            cycle.world.add_material(*MATERIALS[story], "EXPERIMENTAL: материал истории " + story)


def add_input(cycle, story, number):
    text = INPUTS.get((story, number))
    if text is None:
        return
    iid = new_id("inp")
    result = cycle.ctl.record_event(EventSpec("input.arrived", "rx:s25:" + iid, {
        "input_id": iid, "text": text, "sender": "test_author", "channel": "telegram",
        "test_fixture": True,
    }), actor_ref="telegram_receiver")
    if result.event is None:
        raise StopSeries("input_rejected")
    cycle.perception.notice(iid)


def run_series(output, *, mode="stub", auth_file=None, provider_factory=None):
    if mode not in ("stub", "live"):
        raise ValueError("Unknown mode")
    if mode == "live" and not auth_file:
        raise ValueError("Live mode requires a private auth file")
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("A new output directory is required")
    auth = Path(auth_file).expanduser().resolve() if auth_file else None
    if auth is not None and auth.is_relative_to(output):
        raise ValueError("Credentials must be outside evidence")
    private = [str(auth)] if auth is not None else []
    artifacts = audit.Artifacts(output, private_paths=private)
    output.mkdir(parents=True, mode=0o700)
    frozen = sources()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    initial = {}
    for story in "ABCDE":
        baseline = output / f"initial/{story}.db"
        initial_database(baseline, story)
        initial[story] = {"db": f"initial/{story}.db", "sha256": sha(baseline),
                          "history_digest": state_digest(baseline)}
    if sources() != frozen:
        raise StopSeries("sources_changed_during_preparation")
    manifest = {"version": VERSION, "model": MODEL, "mode": mode, "commit": commit, "sources": frozen,
                "python": platform.python_version(), "initial": initial, "profile": PROFILE,
                "profile_sha256": sha(PROFILE_PATH), "materials": MATERIALS,
                "inputs": [{"story": key[0], "session": key[1], "text": text} for key, text in INPUTS.items()],
                "pairs": PAIRS, "stories": list("ABCDE"), "sessions": 3, "decisions_per_session": 2,
                "budgets": {"decisions": MAX_DECISIONS, "model_calls": MAX_CALLS,
                            "calls_per_session": 10, "session_ms": 1_800_000,
                            "calls_per_decision": FORECAST, "native_format_retries": 1,
                            "cycle_format_repairs": 1, "stale_retries": 1},
                "cycle": {"max_output_tokens": 800, "deadline_ms": 60_000, "retrieval_limit": 5,
                          "max_rerequest": 1, "memory_working_limit": 8},
                "prepared_choices": 0, "initial_intentions": 0, "initial_interpretations": 0,
                "behavioral_success": None}
    artifacts.write("manifest.json", manifest, exclusive=True)
    manifest_sha = sha(output / "manifest.json")
    summary = {"version": VERSION, "mode": mode, "status": "running", "stop": None,
               "decisions": 0, "model_calls": 0, "attempts": [], "sessions": [],
               "behavioral_success": None, "manifest_sha256": manifest_sha}

    def save():
        artifacts.write("summary.json", summary)

    def check_frozen():
        if sources() != frozen or sha(output / "manifest.json") != manifest_sha:
            raise StopSeries("frozen_sources_changed")

    def run_session(db, story, number, name, *, switch=None):
        check_frozen()
        with Store.open(db) as store:
            ctl = StateController(store)
            repair = audit.FormatRepairBudget()
            active = {"session": None}
            record = {"name": name, "story": story, "session": number, "switch": switch,
                      "decisions": [], "before_digest": state_digest_from_open(store)}
            summary["sessions"].append(record)
            save()

            def transport(argv, stdin, timeout, cleanup=None):
                repair.before_transport(native_repeat=getattr(inner, "last_calls", 0) > 0)
                if summary["model_calls"] >= MAX_CALLS:
                    raise StopSeries("global_calls")
                session = active["session"]
                session.charge()
                attempt = {"index": summary["model_calls"] + 1, "session": name,
                           "request_id": session.request_id, "status": "reserved"}
                summary["attempts"].append(attempt)
                summary["model_calls"] += 1
                save()
                try:
                    remaining = (session.config()["max_ms"] - session.usage()["elapsed_ms"]) / 1000
                    result = subprocess_runner(argv, stdin, max(.001, min(timeout, remaining)), cleanup)
                except Exception as exc:
                    attempt.update(status="error", error_class=type(exc).__name__)
                    save()
                    raise
                attempt.update(status="returned", returncode=result.returncode, timed_out=result.timed_out)
                save()
                return result

            if provider_factory is not None:
                inner = provider_factory(name, transport)
            elif mode == "live":
                inner = CodexProvider(transport, CodexSandboxConfig(auth_file=str(auth), persist_auth_updates=True),
                                      model=MODEL, timeout_s=60, max_format_retries=1)
            else:
                inner = ScriptedProvider(Reply((rest(),)) for _ in range(10))
            provider = audit.AuditedProvider(GuardedProvider(inner, active), artifacts, "sessions/" + name, disclose=False,
                                             repair_budget=repair)
            session, cycle, _ = wire(ctl, provider=provider, charge=mode == "stub",
                                    motivation_off=switch == "motivation", cycle_type=ExperimentCycle)
            active.update(session=session, memory=cycle.memory)
            cycle.memory.off = switch == "memory"
            if ctl.session_state == "paused":
                session.command("stop", "EXPERIMENTAL: закрытие прежнего сеанса")
            if ctl.session_state != "stopped":
                raise StopSeries("baseline_session_running")
            session.command("start", "EXPERIMENTAL: история " + story)
            add_input(cycle, story, number)
            for index in range(2):
                check_frozen()
                usage = session.usage()
                if summary["decisions"] >= MAX_DECISIONS:
                    raise StopSeries("global_decisions")
                if mode == "live" and MAX_CALLS - summary["model_calls"] < FORECAST:
                    raise StopSeries("global_forecast")
                if usage["max_calls"] - usage["calls"] < FORECAST:
                    record["planned_stop"] = "session_forecast"
                    break
                if usage["elapsed_ms"] >= usage["max_ms"] or ctl.session_state != "running":
                    raise StopSeries("session_limit")
                summary["decisions"] += 1
                save()
                repair.begin_decision()
                before = store.last_seq()
                traces = len(provider.traces)
                report = tick(cycle)
                repair.active = False
                events = [event.to_dict() for event in ctl.events_after(before)]
                faults, repaired, _, _ = audit.decision_technical_faults(
                    asdict(report), events, provider.traces[traces:])
                faults = [fault for fault in faults
                          if not (fault["source"] == "event"
                                  and fault["event"]["payload"].get("fault") == "creator_undelivered")]
                appraisal_errors = audit.assessment_errors(events)
                entry = {"index": index + 1, "report": asdict(report), "events": events,
                         "technical_faults": faults, "assessment_errors": appraisal_errors,
                         "resolved_format_events": repaired,
                         "state": ctl.state, "usage": session.usage(), "provider_failure": provider.failure,
                         "traces": provider.traces[traces:], "format_budget": repair.snapshot()}
                artifacts.write(f"sessions/{name}/decisions/{index + 1:02d}.json", entry)
                record["decisions"].append({"index": index + 1, "report": asdict(report)})
                save()
                if verify(store) or store.integrity_problems():
                    raise StopSeries("state_integrity")
                if provider.failure or not any(audit.valid_choice_trace(trace) for trace in entry["traces"]):
                    raise StopSeries("provider_or_empty_choice")
                if faults or appraisal_errors:
                    raise StopSeries("recorded_technical_error")
                if ctl.session_state != "running":
                    raise StopSeries("unexpected_pause")
            session.command("stop", "EXPERIMENTAL: конец сеанса")
            record["after_digest"] = state_digest_from_open(store)
            record["usage"] = session.usage()
            artifacts.write("sessions/" + name + "/final.json", {
                "state": ctl.state, "events": [e.to_dict() for e in store.events()],
                "memories": [r.to_dict() for r in ctl.readable_memories()], "record": record,
            })
            save()

    save()
    try:
        for story in "ABCDE":
            db = output / f"stories/{story}/persona.db"
            baseline = output / initial[story]["db"]
            if sha(baseline) != initial[story]["sha256"]:
                raise StopSeries("initial_baseline_changed")
            db.parent.mkdir(parents=True)
            shutil.copyfile(baseline, db)
            for number in range(1, 4):
                run_session(db, story, number, f"{story}-{number}")
                if (story, number) in {(pair[0], pair[1]) for pair in PAIRS}:
                    baseline = output / f"checkpoints/{story}-{number}.db"
                    baseline.parent.mkdir(exist_ok=True)
                    shutil.copyfile(db, baseline)
                    artifacts.write(f"checkpoints/{story}-{number}.json",
                                    {"sha256": sha(baseline), "history_digest": state_digest(baseline)})
        for story, checkpoint, switch in PAIRS:
            baseline = output / f"checkpoints/{story}-{checkpoint}.db"
            baseline_sha = sha(baseline)
            baseline_digest = state_digest(baseline)
            for repeat in (1, 2):
                for variant in ("full", switch):
                    check_frozen()
                    name = f"{story}-{switch}-{repeat}-{variant}"
                    db = output / f"controls/{name}/persona.db"
                    db.parent.mkdir(parents=True)
                    shutil.copyfile(baseline, db)
                    if sha(db) != baseline_sha or state_digest(db) != baseline_digest:
                        raise StopSeries("control_baseline_mismatch")
                    run_session(db, story, checkpoint + 1, name, switch=None if variant == "full" else switch)
        check_frozen()
        for entry in initial.values():
            if sha(output / entry["db"]) != entry["sha256"]:
                raise StopSeries("initial_baseline_changed")
        for story, checkpoint, _ in PAIRS:
            record = json.loads((output / f"checkpoints/{story}-{checkpoint}.json").read_text())
            if sha(output / f"checkpoints/{story}-{checkpoint}.db") != record["sha256"]:
                raise StopSeries("control_baseline_changed")
        summary["status"] = "completed"
    except Exception as exc:
        summary.update(status="stopped", stop=type(exc).__name__,
                       diagnostic=artifacts.clean(str(exc)))
    save()
    return summary


def state_digest_from_open(store):
    return hashlib.sha256(json.dumps({"state": store.latest_snapshot().state,
                                      "events": [event.to_dict() for event in store.events()]},
                         sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("stub", "live"), default="stub")
    parser.add_argument("--auth-file")
    args = parser.parse_args()
    result = run_series(args.output, mode=args.mode, auth_file=args.auth_file)
    print(json.dumps({key: result[key] for key in ("status", "stop", "decisions", "model_calls")}))
    return int(result["status"] != "completed")


if __name__ == "__main__":
    raise SystemExit(main())
