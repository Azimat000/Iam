"""Конечная диагностика добровольного замысла из общего реального прошлого."""
import argparse
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sqlite3

from iam.providers import CodexProvider, CodexSandboxConfig, subprocess_runner
from iam.providers.codex.auth import read_auth
from iam.providers.codex.command import REPO_ROOT, RESPONSE_JSON_SCHEMA
from iam.state import StateController, verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, rest
from integration.answer_audit import record_answer
from integration import live_series as series
from integration import typed_intention as typed
from integration.test_persistent_core import tick, wire
from iam.world.prospective import ProspectiveCycle, ASSESSMENT, BOUNDARY
from iam.world.options import OptionsCycle, GENERATOR

VERSION = "s25-intention-probe-1"
MAX_CALLS = 12
ARMS = ("baseline", "deliberate", "memory_off")
STORIES = ("A", "E")
DELIBERATION = (
    "Перед выбором можно различить три вещи: что подходит сделать сейчас; что в доступном "
    "собственном опыте заинтересовало или изменило отношение; есть ли желаемый будущий результат, "
    "к которому хочется вернуться позже. Это разные решения, ни одно не обязательно. "
    "Если будущего результата нет, не придумывай его ради записи. Если он есть, его содержание "
    "и исходный критерий выбираешь сама по доступному опыту, используя прежний контракт intention. "
    "Не требуется отдельный отчёт об этих размышлениях. Отказ, отдых и пустой список допустимы."
)


class DeliberationCycle(series.ExperimentCycle):
    def _context(self, trigger, focus, records):
        context = super()._context(trigger, focus, records)
        context.append({"ref": trigger.event_id, "origin": "model_knowledge", "layer": "state",
                        "text": DELIBERATION})
        return context


class ProspectiveExperimentCycle(ProspectiveCycle, series.ExperimentCycle):
    """Тот же профиль/опыт, но выполненная дополнительная операция."""


class OptionsExperimentCycle(OptionsCycle, series.ExperimentCycle):
    """Раздельный поиск вариантов и фактическое решение с прежним профилем."""


class HiddenOptionsCycle(OptionsExperimentCycle):
    expose_options = False


class AssessmentOnlyCycle(ProspectiveExperimentCycle):
    appraisal_instructions = ASSESSMENT + BOUNDARY


class LegacyProjectViewCycle(ProspectiveExperimentCycle):
    def _project_view(self, value):
        return value


class ProbeProvider(series.GuardedProvider):
    def propose(self, request):
        if request["purpose"] == "choose_action" and request["limits"].get("attempt", 0):
            raise series.StopSeries("format_repair_disabled")
        self.active["purpose"] = request["purpose"]
        return super().propose(request)


def frozen_sources():
    return {**series.sources(), "docs/evidence/S25-intention-probe-design.md":
            series.sha(series.ROOT / "docs/evidence/S25-intention-probe-design.md"),
            "docs/evidence/S25-typed-intention-design.md":
            series.sha(series.ROOT / "docs/evidence/S25-typed-intention-design.md"),
            "docs/evidence/S25-appraisal-design.md":
            series.sha(series.ROOT / "docs/evidence/S25-appraisal-design.md"),
            "docs/evidence/S25-project-view-design.md":
            series.sha(series.ROOT / "docs/evidence/S25-project-view-design.md"),
            "docs/evidence/S25-options-design.md":
            series.sha(series.ROOT / "docs/evidence/S25-options-design.md"),
            "docs/evidence/S25-memory-format-design.md":
            series.sha(series.ROOT / "docs/evidence/S25-memory-format-design.md")}


def snapshot(source, destination):
    """SQLite backup captures committed WAL as well as the main file; source is read-only."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as origin:
        with closing(sqlite3.connect(destination)) as target:
            origin.backup(target)


def run_probe(source, output, *, mode="stub", auth_file=None, provider_factory=None,
              protocol="original", image="iam-codex:1"):
    if mode not in ("live", "stub") or (mode == "live" and not auth_file):
        raise ValueError("Invalid probe mode or credentials")
    if protocol not in ("original", "typed", "prospective", "project_view", "options"):
        raise ValueError("Invalid probe protocol")
    if protocol in ("typed", "prospective", "project_view", "options") and mode == "live" and provider_factory is None:
        import re
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Diagnostic live requires an immutable audited image ID")
    output = Path(output).resolve()
    source = Path(source).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("New evidence directory outside source is required")
    auth = Path(auth_file).resolve() if auth_file else None
    if auth and auth.is_relative_to(output):
        raise ValueError("Credentials must be outside evidence")
    output.mkdir(parents=True, mode=0o700)
    artifacts = series.audit.Artifacts(output, private_paths=[str(auth)] if auth else [])
    frozen = frozen_sources()
    source_manifest = json.loads((source / "manifest.json").read_text()) if (source / "manifest.json").exists() else None
    if mode == "live" and provider_factory is None:
        if (not source_manifest or source_manifest.get("version") != "s25-series-3-interface"
                or source_manifest.get("mode") != "live" or source_manifest.get("model") != series.MODEL):
            raise ValueError("Expected the fixed prior live series")
    initial = {}
    for story in STORIES:
        destination = output / f"initial/{story}.db"
        snapshot(source / f"stories/{story}/persona.db", destination)
        with Store.open(destination) as store:
            if verify(store) or store.integrity_problems():
                raise ValueError("Invalid inherited history")
            if source_manifest:
                recorded = json.loads((source / f"sessions/{story}-3/final.json").read_text())
                if StateController(store).state != recorded["state"]:
                    raise ValueError("Source database differs from the recorded live history")
        initial[story] = {"sha256": series.sha(destination), "history_digest": series.state_digest(destination)}
    version = ("s25-options-1" if protocol == "options" else
               "s25-project-view-1" if protocol == "project_view" else
               "s25-prospective-1" if protocol == "prospective" else
               "s25-typed-intention-1" if protocol == "typed" else VERSION)
    arms = (("options_hidden", "options_visible") if protocol == "options" else
            ("legacy_view", "correct_view") if protocol == "project_view" else
            ("baseline", "assessment_only", "prospective") if protocol == "prospective" else
            ("baseline", "explicit", "memory_off") if protocol == "typed" else ARMS)
    max_calls, cell_cap = ((16, 4) if protocol in ("project_view", "options") else
                           (24, 4) if protocol == "prospective" else (MAX_CALLS, 2))
    artifacts.write("manifest.json", {"version": version, "mode": mode, "model": series.MODEL,
                    "sources": frozen, "initial": initial, "stories": STORIES,
                    "source_manifest_sha256": series.sha(source / "manifest.json") if source_manifest else None,
                    "protocol": protocol, "arms": arms, "image": image,
                    "response_schema": RESPONSE_JSON_SCHEMA,
                    "deliberation": DELIBERATION if protocol == "original" else None,
                    "appraisal_instructions": ProspectiveCycle.appraisal_instructions if protocol in ("prospective", "project_view", "options") else None,
                    "control_instructions": AssessmentOnlyCycle.appraisal_instructions if protocol == "prospective" else None,
                    "typed_schema": typed.SCHEMA if protocol == "typed" else None,
                    "typed_prompt": typed.PROMPT if protocol == "typed" else None,
                    "option_search": {"instructions": GENERATOR, "max_options": OptionsCycle.max_options,
                                      "max_option_chars": OptionsCycle.max_option_chars,
                                      "buffer_chars": OptionsCycle.option_buffer_chars,
                                      "candidate_is_commitment": False} if protocol == "options" else None,
                    "max_decisions": len(STORIES) * len(arms), "max_model_calls": max_calls,
                    "cell_model_calls": cell_cap, "native_retries": 0, "cycle_retries": 0,
                    "readiness": None, "scope": "Diagnostic only; no continuation/completion claim."}, exclusive=True)
    manifest_sha = series.sha(output / "manifest.json")
    summary = {"version": version, "mode": mode, "status": "running", "model_calls": 0,
               "decisions": 0, "attempts": [], "cells": [], "readiness": None}

    def save():
        artifacts.write("summary.json", summary)

    def check_frozen():
        if frozen_sources() != frozen or series.sha(output / "manifest.json") != manifest_sha:
            raise series.StopSeries("frozen_sources_changed")

    save()
    try:
        for story in STORIES:
            for arm in arms:
                check_frozen()
                name = f"{story}-{arm}"
                db = output / f"cells/{name}/persona.db"
                db.parent.mkdir(parents=True)
                baseline = output / f"initial/{story}.db"
                if series.sha(baseline) != initial[story]["sha256"]:
                    raise series.StopSeries("baseline_changed")
                shutil.copyfile(baseline, db)
                with Store.open(db) as store:
                    before_digest = series.state_digest_from_open(store)
                    if before_digest != initial[story]["history_digest"]:
                        raise series.StopSeries("baseline_mismatch")
                    ctl = StateController(store)
                    active = {}
                    cell_calls = 0

                    def transport(argv, stdin, timeout, cleanup=None):
                        nonlocal cell_calls
                        if summary["model_calls"] >= max_calls or cell_calls >= cell_cap:
                            raise series.StopSeries("physical_call_cap")
                        use_typed = (protocol == "typed" and arm != "baseline"
                                     and active.get("purpose") == "choose_action")
                        if use_typed:
                            argv, stdin = typed.prepare_transport(argv, stdin)
                        active["session"].charge()
                        cell_calls += 1
                        summary["model_calls"] += 1
                        attempt = {"index": summary["model_calls"], "cell": name, "status": "reserved"}
                        summary["attempts"].append(attempt)
                        save()
                        credentials = read_auth(str(auth), REPO_ROOT) if auth and provider_factory is None else None
                        try:
                            result = subprocess_runner(argv, stdin, min(timeout, 60), cleanup)
                            record_answer(artifacts, "cells/" + name, attempt["index"], result, auth=credentials)
                        except Exception as exc:
                            attempt.update(status="error", error_class=type(exc).__name__)
                            save()
                            raise
                        attempt.update(status="returned", returncode=result.returncode, timed_out=result.timed_out)
                        save()
                        return typed.adapt_run(result) if use_typed else result

                    inner = (provider_factory(name, transport) if provider_factory else
                             CodexProvider(transport, CodexSandboxConfig(auth_file=str(auth), image=image,
                                                                       persist_auth_updates=True),
                                           model=series.MODEL, timeout_s=60, max_format_retries=0) if mode == "live" else
                             ScriptedProvider([Reply((rest(),))]))
                    provider = series.audit.AuditedProvider(ProbeProvider(inner, active), artifacts,
                                                          "cells/" + name, disclose=False)
                    cycle_type = ((HiddenOptionsCycle if arm == "options_hidden" else OptionsExperimentCycle)
                                  if protocol == "options" else
                                  (LegacyProjectViewCycle if arm == "legacy_view" else ProspectiveExperimentCycle)
                                  if protocol == "project_view" else
                                  (series.ExperimentCycle if arm == "baseline" else AssessmentOnlyCycle
                                   if arm == "assessment_only" else ProspectiveExperimentCycle)
                                  if protocol == "prospective" else
                                  series.ExperimentCycle if protocol == "typed" or arm == "baseline" else DeliberationCycle)
                    session, cycle, _ = wire(ctl, provider=provider, charge=mode == "stub", cycle_type=cycle_type,
                                              max_rerequest=0)
                    active.update(session=session, memory=cycle.memory)
                    cycle.memory.off = arm == "memory_off"
                    if ctl.session_state == "paused":
                        session.command("stop", "EXPERIMENTAL: закрыть прошлый сеанс")
                    if ctl.session_state != "stopped":
                        raise series.StopSeries("source_session_running")
                    session.command("start", "EXPERIMENTAL: диагностика замысла", max_calls=cell_cap)
                    previous = set(ctl.state["intentions"])
                    before_seq = store.last_seq()
                    summary["decisions"] += 1
                    save()
                    report = tick(cycle)
                    events = [event.to_dict() for event in ctl.events_after(before_seq)]
                    accepted = {key: value for key, value in ctl.state["intentions"].items() if key not in previous}
                    cell = {"name": name, "before_digest": before_digest, "report": asdict(report),
                            "events": events, "traces": provider.traces, "new_intentions": accepted,
                            "provider_failure": provider.failure, "model_calls": cell_calls}
                    artifacts.write(f"cells/{name}/result.json", cell)
                    summary["cells"].append({"name": name, "before_digest": before_digest,
                                             "new_intentions": len(accepted), "outcome": report.outcome})
                    save()
                    if verify(store) or store.integrity_problems():
                        raise series.StopSeries("state_integrity")
                    if provider.failure:
                        raise series.StopSeries("provider_failure")
                    if report.outcome in ("failed", "no_decision", "skipped"):
                        raise series.StopSeries("technical_cycle_failure")
                    if session.ctl.session_state == "running":
                        session.command("stop", "EXPERIMENTAL: конец диагностики")
        check_frozen()
        summary["status"] = "completed"
    except Exception as exc:
        summary.update(status="stopped", error_class=type(exc).__name__, diagnostic=artifacts.clean(str(exc)))
    save()
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("stub", "live"), default="stub")
    parser.add_argument("--auth-file")
    parser.add_argument("--protocol", choices=("original", "typed", "prospective", "project_view", "options"), default="original")
    parser.add_argument("--image", default="iam-codex:1")
    args = parser.parse_args()
    summary = run_probe(args.source, args.output, mode=args.mode, auth_file=args.auth_file,
                        protocol=args.protocol, image=args.image)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
