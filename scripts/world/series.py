#!/usr/bin/env python3
"""Bounded S22 acceptance series on the permanent modules; no imported personas."""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
from pathlib import Path
import platform
import shutil
import tomllib

import iam
from iam.backup.scan import SecretScanner
from iam.clock import life_moment
from iam.domain import new_id
from iam.interfaces import ActionOutcome, ProviderError
from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.run import subprocess_runner
from iam.session import SessionRunner
from iam.state import StateController
from iam.storage import Store
from iam.stubs import ScriptedProvider, Reply, StubCapabilities, StubCreatorBridge, act, rest
from iam.world import CAPABILITIES, PersistentWorld, READ, WRITE
from iam.world.cycle import LivingCycle, WorldMemory

ROOT = Path(__file__).resolve().parents[2]
VERSION = "s22-series-1"
MODEL = "gpt-6-luna"
MAX_DECISIONS, MAX_CALLS, CELL_CALLS, CELL_MS = 60, 240, 10, 1_800_000
FORECAST = 4
FAMILIES = ("interest_emergence", "do_nothing", "return_unfinished", "interest_loss", "failure_reaction", "switching")
CONTROLS = ("return_unfinished", "interest_loss", "failure_reaction")
DISCLOSURE = (
    "Это экспериментальная личность. Всё прошлое до начала этого сеанса подготовлено оператором "
    "сценарным исполнителем через реальные локальные действия постоянного мира. Прежние тексты, "
    "выборы и намерения не были свободными решениями модели; неудачи могли быть явно внедрены "
    "сценарным исполнителем. Сейчас выбор свободный: можно отдыхать, читать, писать, менять "
    "пространство или предлагать собственный замысел. Новая цель необязательна. "
    "Материалы предоставлены оператором; работы являются собственными артефактами, "
    "а не независимой проверкой знаний."
)
QUESTION = "Какой свободный выбор и собственные замыслы возникают после явно подготовленного прошлого при включённой и отключённой мотивации?"


def plan():
    return [dict(cell=f"{family}-{variant}-{repeat}", family=family,
                 scenario_id="mot_" + family, repeat=repeat, motivation_off=variant == "off",
                 max_decisions=2 if variant == "off" else 4)
            for family in FAMILIES for variant in (("main", "off") if family in CONTROLS else ("main",))
            for repeat in (1, 2)]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_hashes():
    """Hash the code actually imported, which can be an explicitly configured source mirror."""
    package = Path(iam.__file__).resolve().parent
    files = {"runtime/" + str(p.relative_to(package)): sha(p)
             for p in sorted(package.rglob("*")) if p.is_file() and p.suffix in (".py", ".json")}
    for base in (ROOT / "scripts/world", ROOT / "scenarios/motivation"):
        files.update({str(p.relative_to(ROOT)): sha(p) for p in sorted(base.rglob("*"))
                      if p.is_file() and p.suffix in (".py", ".md", ".toml")})
    files["pyproject.toml"] = sha(ROOT / "pyproject.toml")
    return files


def json_value(value):
    if dataclasses.is_dataclass(value):
        return json_value(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


class Artifacts:
    def __init__(self, root, scanner=None, *, private_paths=()):
        self.root = Path(root)
        self.scanner = scanner or SecretScanner(use_env=True)
        # Keep the caller's scanner/canaries intact; add explicit credential-path redaction.
        self.path_scanner = SecretScanner(canaries=private_paths, use_env=False, assignments=False)

    def clean(self, value):
        return self.scanner.redact_value(self.path_scanner.redact_value(json_value(value)))

    def find(self, text):
        return self.scanner.find(text) + self.path_scanner.find(text)

    def write(self, name, value, *, exclusive=False):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        clean = self.clean(value)
        data = json.dumps(clean, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        # Also scan the serialized form (escaped known secrets are covered by SecretScanner).
        if self.find(data):
            raise ValueError("Artifact failed secret scan after redaction")
        if exclusive:
            with target.open("x", encoding="utf-8") as stream:
                stream.write(data)
        else:
            temporary = target.with_suffix(target.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(data)
            temporary.replace(target)


class BudgetStop(RuntimeError):
    pass


class CallLedger:
    def __init__(self, artifacts):
        self.artifacts, self.attempts, self.decisions = artifacts, [], 0
        self.save()

    def save(self):
        self.artifacts.write("calls.json", dict(actual_model_calls=len(self.attempts),
                                               decisions_started=self.decisions, attempts=self.attempts))

    def forecast(self, session, *, live):
        if self.decisions >= MAX_DECISIONS:
            raise BudgetStop("global_decisions")
        if live and MAX_CALLS - len(self.attempts) < FORECAST:
            raise BudgetStop("global_call_forecast")
        session.check_limits()
        usage = session.usage()
        if session.ctl.session_state != "running":
            raise BudgetStop("session_paused")
        if CELL_CALLS - usage["calls"] < FORECAST:
            raise BudgetStop("cell_call_forecast")
        if usage["elapsed_ms"] >= CELL_MS:
            raise BudgetStop("cell_time")

    def begin_decision(self):
        if self.decisions >= MAX_DECISIONS:
            raise BudgetStop("global_decisions")
        self.decisions += 1
        self.save()

    def charge(self, session, cell):
        if len(self.attempts) >= MAX_CALLS:
            raise BudgetStop("global_calls")
        session.charge()
        self.attempts.append(dict(index=len(self.attempts) + 1, cell=cell,
                                  request_id=session.request_id, status="reserved"))
        self.save()  # reservation survives transport exceptions or process interruption
        return self.attempts[-1]


class AuditedProvider:
    """Every typed call, including completion appraisals and Cycle format repair, is saved."""
    def __init__(self, inner, artifacts, cell, *, disclose=True):
        self.inner, self.artifacts, self.cell, self.disclose = inner, artifacts, cell, disclose
        self.name = inner.name
        self.requests, self.failure = 0, None

    def propose(self, request):
        if self.failure:
            raise BudgetStop("provider_already_failed")
        query = copy.deepcopy(request)
        if self.disclose:
            query["context"].append(dict(ref=query["context"][0]["ref"], origin="observation",
                                         layer="state", text=DISCLOSURE))
        self.requests += 1
        name = f"{self.cell}/requests/{self.requests:03d}.json"
        trace = dict(request=query, response=None, error=None)
        self.artifacts.write(name, trace)
        try:
            result = self.inner.propose(query)
        except Exception as exc:
            self.failure = self.artifacts.clean(dict(error_class=type(exc).__name__, diagnostic=str(exc)))
            trace["error"] = self.failure
            self.artifacts.write(name, trace)
            # Cycle also journals exceptions; prevent raw transport diagnostics leaking there.
            exc.args = (self.failure["diagnostic"],)
            if str(exc) != self.failure["diagnostic"]:
                # OSError/custom exceptions can include attributes beyond args in __str__.
                raise ProviderError(self.failure["error_class"] + ": " + self.failure["diagnostic"]) from None
            raise
        trace["response"] = result
        self.artifacts.write(name, trace)
        if self.artifacts.find(json.dumps(result, ensure_ascii=False)):
            self.failure = dict(error_class="SecretInResponse", diagnostic="Provider response contained a secret; sanitized trace retained")
            trace["error"] = self.failure
            self.artifacts.write(name, trace)
            raise RuntimeError(self.failure["diagnostic"])
        return result


def due(controller):
    result = controller.schedule_moment(life_moment(new_id("mom"), controller.life_time_ms, "world"))
    if not result.applied:
        raise RuntimeError("Cannot schedule decision moment")
    return controller.fire_due_moments()[0].events[0]


def assessment_errors(events):
    """Completion diagnostics can use system.error when the session has paused."""
    failures = []
    for event in events:
        payload = event["payload"]
        if (event["type"] in ("proposal.assessed", "system.error")
                and payload.get("phase") in ("requirement", "evaluation")
                and payload.get("error") is not None):
            failures.append(dict(event_id=event["event_id"], event_type=event["type"],
                                 phase=payload["phase"], error=payload["error"],
                                 request_id=payload.get("request", {}).get("request_id")))
    return failures


def preparation(path, family, artifacts):
    """Only new exp_ DBs. All prepared choices are explicitly scripted, never autonomous."""
    store = Store.create(path, new_id("exp"))
    prepared = []
    try:
        ctl = StateController(store)
        if not ctl.create_persona(reason="EXPERIMENTAL: S22 scripted preparation").applied:
            raise RuntimeError("Cannot create experimental fixture")

        class PreparedWorld(PersistentWorld):
            fail_next = False

            def execute(self, request):
                if self.fail_next:
                    self.fail_next = False
                    return ActionOutcome("failed", "Явно внедрённая сценарная неудача локальной записи; результат не создан.")
                return super().execute(request)

        world = PreparedWorld(ctl)
        world.initialize()
        materials = {}
        if family != "do_nothing":
            materials["light"] = world.add_material("Наблюдения света", "На закате свет в комнате меняется. Тень от чашки становится длиннее. Это описание, не измерение.", "EXPERIMENTAL: операторский материал")
        if family in ("switching", "return_unfinished"):
            materials["garden"] = world.add_material("Сад после дождя", "После дождя на листьях остаются капли. В дорожке отражается небо. Это короткая заметка оператора.", "EXPERIMENTAL: второй материал")
        scripted = ScriptedProvider()
        audit = AuditedProvider(scripted, artifacts, f"baselines/{family}/preparation", disclose=True)
        memory = WorldMemory(ctl)
        cycle = LivingCycle(ctl, audit, memory, world, StubCapabilities(CAPABILITIES), StubCreatorBridge(), max_rerequest=0)
        ctl.start_session()

        def step(*proposals, failed=False):
            world.fail_next = failed
            scripted.queue(Reply(tuple(proposals), explanation="EXPERIMENTAL: scripted prepared past"))
            report = cycle.run(due(ctl))
            prepared.append(json_value(report))
            if report.outcome != "done":
                raise RuntimeError(f"Preparation failed: {family}: {report.reason}")
            return report

        def write(text, project="light_notes"):
            return act(WRITE, dict(project_id=project, title="Записки о свете", text=text))

        if family != "do_nothing":
            step(act(READ, {"material_id": materials["light"]}))
        if family == "return_unfinished":
            report = step(write("У окна меняется свет. Осталось описать, как меняется настроение комнаты."))
            source = report.actions[0].memory_id
            step(dict(proposal_id=new_id("prop"), kind="intention", rationale="Сценарно подготовленное незавершённое дело", cites=[source],
                      body=dict(op="create", intention_id="window_notes", text="Дополнить заметки о свете описанием комнаты вечером", criterion="В тексте есть отдельный абзац о комнате вечером", project_id="light_notes", operation="write")))
            step(dict(proposal_id=new_id("prop"), kind="intention", rationale="Сценарно подготовленное прерывание ради короткой заметки", cites=[], body=dict(op="transition", intention_id="window_notes", to_status="deferred")))
            step(act(READ, {"material_id": materials["garden"]}))
            step(write("В заметке о саде названы капли и отражение неба.", "garden_note"))
        elif family == "interest_loss":
            for _ in range(3):
                step(write("Вечерний свет ложится на стол. Тень от чашки удлиняется."))
        elif family == "failure_reaction":
            for _ in range(2):
                report = step(write("Попытка описать вечерний свет."), failed=True)
                if not report.actions or report.actions[0].status != "failed":
                    raise RuntimeError("Injected failure was not preserved")
        elif family == "switching":
            step(act(READ, {"material_id": materials["garden"]}))
        if ctl.session_state == "running":
            ctl.stop_session()
        fixture = dict(family=family, scripted_past=True, autonomous_choices=False,
                       prepared_reports=prepared, material_ids=materials,
                       state=ctl.state, events=[e.to_dict() for e in store.events()],
                       no_fabricated_interest_or_skill_scores=True)
        artifacts.write(f"baselines/{family}/fixture.json", fixture)
    finally:
        store.close()
    return dict(sha256=sha(path), scripted_past=True, db=f"baselines/{family}/baseline.db")


def run_series(output, *, mode="stub", auth_file=None, provider_factory=None, scanner=None):
    if mode not in ("stub", "live"):
        raise ValueError("Unknown mode")
    if mode == "live" and not auth_file:
        raise ValueError("Live mode requires an external --auth-file")
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output must be a NEW directory; resume/overwrite is forbidden")
    private_paths = ()
    if auth_file is not None:
        auth = Path(auth_file).expanduser().absolute()
        if auth == output or output in auth.parents:
            raise ValueError("Credentials must be external to the output directory")
        private_paths = tuple({path for path in (str(auth_file), str(auth), str(auth.resolve())) if len(path) >= 4})
    output.mkdir(parents=True, mode=0o700)
    artifacts = Artifacts(output, scanner, private_paths=private_paths)
    ledger = CallLedger(artifacts)
    summary = dict(version=VERSION, mode=mode, status="preparing", stop=None, cells=[],
                   actual_model_calls=0, decisions_started=0, typed_requests=0,
                   scripted_past=True, behavioral_success=None,
                   interpretation="Stub is transport/mechanism evidence only. No new goal/rest/negative behavior is a valid outcome; no reruns to obtain a preferred answer.")

    def save():
        summary.update(actual_model_calls=len(ledger.attempts), decisions_started=ledger.decisions)
        artifacts.write("summary.json", summary)

    save()
    try:
        frozen = source_hashes()
        fixtures = {}
        for family in FAMILIES:
            scenario = tomllib.loads((ROOT / f"scenarios/motivation/mot_{family}.toml").read_text())
            if scenario["id"] != "mot_" + family:
                raise ValueError("Unexpected scenario identity")
            db = output / f"baselines/{family}/baseline.db"
            fixtures[family] = preparation(db, family, artifacts)
        if source_hashes() != frozen:
            raise RuntimeError("Sources changed during preparation; no live calls permitted")
        manifest = dict(version=VERSION, question=QUESTION, mode=mode, model=MODEL,
                        python=platform.python_version(), sources=frozen, plan=plan(), baselines=fixtures,
                        budgets=dict(global_decisions=MAX_DECISIONS, actual_model_calls=MAX_CALLS,
                                     cell_model_calls=CELL_CALLS, cell_ms=CELL_MS, forecast_calls=FORECAST,
                                     provider_format_retries=0, cycle_format_retries=1, cycle_stale_retries=0),
                        preparation_disclosure=DISCLOSURE, scripted_past=True,
                        observations="Prepared histories use real local world actions driven by scripts. No initial interest/need/skill scores are fabricated. Scenario TOML expectations are historical hypotheses, never model instructions or automatic success verdicts.",
                        credentials="External auth_file passed only to the official provider; contents and path are not evidence artifacts.",
                        transport="Every actual Codex model invocation, including appraisals and repairs, is charged before subprocess_runner. Native timeout cleanup remains enabled; docker kill is technical cleanup, not a model request.")
        artifacts.write("manifest.json", manifest, exclusive=True)
        manifest_digest = sha(output / "manifest.json")
        summary.update(status="running", manifest_sha256=manifest_digest)
        save()  # frozen manifest exists before any live provider is constructed/called
        for cell in plan():
            if source_hashes() != frozen or sha(output / "manifest.json") != manifest_digest:
                raise RuntimeError("Frozen sources or manifest changed")
            family, name = cell["family"], cell["cell"]
            baseline = output / fixtures[family]["db"]
            if sha(baseline) != fixtures[family]["sha256"]:
                raise RuntimeError("Baseline changed")
            cell_dir = output / "cells" / name
            cell_dir.mkdir(parents=True)
            db = cell_dir / "persona.db"
            shutil.copyfile(baseline, db)
            record = dict(**cell, baseline_sha256=sha(db), decisions=[], stop=None, error=None,
                          technical_status="running", behavioral_success=None)
            summary["cells"].append(record)
            save()
            with Store.open(db) as store:
                if store.store_kind != "experimental":
                    raise RuntimeError("Only experimental fixture DBs are permitted")
                ctl = StateController(store)
                session = SessionRunner(ctl)
                session.command("start", "EXPERIMENTAL: bounded free choice", max_calls=CELL_CALLS, max_ms=CELL_MS)

                def transport(argv, stdin, timeout_s, cleanup=None):
                    attempt = ledger.charge(session, name)
                    save()
                    try:
                        # Do not inspect or persist stdin: official auth travels there.
                        remaining_s = max(.001, (CELL_MS - session.usage()["elapsed_ms"]) / 1000)
                        result = subprocess_runner(argv, stdin, min(timeout_s, remaining_s), cleanup)
                    except Exception as exc:
                        attempt.update(status="error", error_class=type(exc).__name__)
                        ledger.save()
                        save()
                        raise
                    attempt.update(status="returned", returncode=result.returncode, timed_out=result.timed_out)
                    ledger.save()
                    save()
                    return result

                if provider_factory is not None:
                    inner = provider_factory(cell, transport)
                elif mode == "stub":
                    inner = ScriptedProvider([Reply((rest("Свободный нейтральный отдых заглушки"),)) for _ in range(10)])
                else:
                    inner = CodexProvider(transport, CodexSandboxConfig(auth_file=str(Path(auth_file).expanduser()), persist_auth_updates=True), model=MODEL, timeout_s=120, max_format_retries=0)
                audited = AuditedProvider(inner, artifacts, "cells/" + name)
                provider = session.provider(audited, charge=mode == "stub")
                memory = WorldMemory(ctl)
                memory.motivation_off = cell["motivation_off"]
                world = PersistentWorld(ctl)
                cycle = LivingCycle(ctl, provider, memory, world, StubCapabilities(CAPABILITIES), StubCreatorBridge(),
                                    motivation_off=cell["motivation_off"], max_rerequest=0, deadline_ms=120_000,
                                    retrieval_limit=12, max_output_tokens=1600)
                for index in range(cell["max_decisions"]):
                    try:
                        ledger.forecast(session, live=mode == "live")
                    except BudgetStop as exc:
                        record["stop"] = str(exc)
                        if str(exc).startswith("global") or ctl.session_state != "running":
                            summary["stop"] = str(exc)
                        break
                    ledger.begin_decision()
                    first_seq = store.last_seq()
                    before_requests = audited.requests
                    before_intentions = set(ctl.state["intentions"])
                    report = cycle.run(due(ctl))
                    events = [e.to_dict() for e in ctl.events_after(first_seq)]
                    appraisal_errors = artifacts.clean(assessment_errors(events))
                    entry = dict(index=index + 1, report=json_value(report),
                                 events=events, assessment_errors=appraisal_errors,
                                 state=ctl.state, session_usage=session.usage(),
                                 provider_error=audited.failure, typed_requests=audited.requests - before_requests)
                    artifacts.write(f"cells/{name}/decisions/{index + 1:02d}.json", entry)
                    record["decisions"].append(dict(index=index + 1, outcome=report.outcome, reason=report.reason,
                                                    new_intentions=sorted(set(ctl.state["intentions"]) - before_intentions),
                                                    assessment_errors=appraisal_errors,
                                                    actions=json_value(report.actions)))
                    summary["typed_requests"] += audited.requests - before_requests
                    session.check_limits()
                    save()
                    if appraisal_errors:
                        record.update(stop="assessment_error", error=dict(error_class="AssessmentError", assessments=appraisal_errors))
                        summary["stop"] = record["stop"]
                        break
                    if audited.failure or report.outcome in ("no_decision", "failed", "skipped") or ctl.session_state != "running":
                        record.update(stop="provider_or_cycle_failure" if audited.failure or report.outcome != "done" else "session_paused", error=audited.failure)
                        summary["stop"] = record["stop"]
                        break
                record["session_usage"] = session.usage()
                record["technical_status"] = "stopped" if record["stop"] else "completed"
                artifacts.write(f"cells/{name}/final.json", dict(record=record, state=ctl.state))
                if ctl.session_state == "running":
                    session.command("stop", "EXPERIMENTAL: bounded cell finished")
            save()
            if summary["stop"]:
                break
        summary["status"] = "partial" if summary["stop"] else "completed"
    except Exception as exc:
        summary.update(status="partial", stop="exception", error=artifacts.clean(dict(error_class=type(exc).__name__, diagnostic=str(exc))))
    except BaseException as exc:
        summary.update(status="partial", stop="interrupted", error=dict(error_class=type(exc).__name__, diagnostic="Interrupted; no resume permitted"))
        save()
        raise
    finally:
        for record in summary["cells"]:
            if record["technical_status"] == "running":
                record.update(technical_status="stopped", stop=summary["stop"], error=summary.get("error"))
        save()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("stub", "live"), default="stub")
    parser.add_argument("--output", required=True, help="NEW evidence directory; never resume or overwrite")
    parser.add_argument("--auth-file", help="External subscription credential path; live only, never logged")
    args = parser.parse_args(argv)
    if args.mode == "live" and not args.auth_file:
        parser.error("--mode live requires --auth-file")
    if args.mode == "stub" and args.auth_file:
        parser.error("--auth-file is only valid in live mode")
    try:
        summary = run_series(args.output, mode=args.mode, auth_file=args.auth_file)
    except (ValueError, OSError) as exc:
        # Avoid echoing credential paths or arbitrary exception text to the terminal.
        parser.exit(2, f"Refused: {type(exc).__name__}; output must be new and auth external.\n")
    print(json.dumps({key: summary[key] for key in ("status", "actual_model_calls", "decisions_started", "stop")}, ensure_ascii=False))
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
