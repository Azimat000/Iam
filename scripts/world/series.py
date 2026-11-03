#!/usr/bin/env python3
"""Bounded S22 acceptance series on the permanent modules; no imported personas."""
from __future__ import annotations

import argparse
import copy
from contextlib import closing
import dataclasses
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import tomllib

import iam
from iam.backup.scan import SecretScanner
from iam.clock import life_moment
from iam.cycle.validator import tool_use_signs
from iam.domain import canonical_json, jsonschema, new_id
from iam.interfaces import ActionOutcome, ProviderError
from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.errors import ProviderFormatError
from iam.providers.run import subprocess_runner
from iam.session import SessionRunner
from iam.state import StateController
from iam.storage import Store
from iam.stubs import ScriptedProvider, Reply, StubCapabilities, StubCreatorBridge, act, rest
from iam.world import CAPABILITIES, PersistentWorld, READ, WRITE
from iam.world.cycle import GUIDANCE, LivingCycle, WorldMemory

ROOT = Path(__file__).resolve().parents[2]
VERSION = "s22-series-3"
MODEL = "gpt-6-luna"
NATIVE_FORMAT_RETRIES = 1
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


class FormatRepairBudget:
    """One native/Cycle format repair and at most four physical calls per decision."""
    def __init__(self):
        self.repairs, self.invocations, self.active = [], 0, False

    def begin_decision(self):
        self.repairs = []
        self.invocations = 0
        self.active = True

    def consume(self, kind):
        if not self.active:
            raise BudgetStop("No active decision for format repair")
        if self.repairs:
            raise BudgetStop("Shared format repair budget exhausted")
        self.repairs.append(kind)

    def before_transport(self, *, native_repeat=False):
        if not self.active:
            raise BudgetStop("No active decision for model invocation")
        if self.invocations >= FORECAST:
            raise BudgetStop("Decision physical invocation cap reached")
        if native_repeat:
            self.consume("native")
        self.invocations += 1

    def snapshot(self):
        return dict(repairs=list(self.repairs), physical_invocations=self.invocations,
                    max_repairs=1, max_physical_invocations=FORECAST)


def budgets(*, native_retries=None):
    return dict(global_decisions=MAX_DECISIONS, actual_model_calls=MAX_CALLS,
                cell_model_calls=CELL_CALLS, cell_ms=CELL_MS, forecast_calls=FORECAST,
                provider_format_retries=NATIVE_FORMAT_RETRIES if native_retries is None else native_retries,
                cycle_format_retries=1, cycle_stale_retries=0)


class CallLedger:
    def __init__(self, artifacts, *, attempts=(), decisions=0):
        if type(decisions) is not int or not 0 <= decisions <= MAX_DECISIONS or len(attempts) > MAX_CALLS:
            raise ValueError("Invalid inherited budget")
        self.artifacts, self.attempts, self.decisions = artifacts, copy.deepcopy(list(attempts)), decisions
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
        if usage.get("max_calls", CELL_CALLS) - usage["calls"] < FORECAST:
            raise BudgetStop("cell_call_forecast")
        if usage["elapsed_ms"] >= usage.get("max_ms", CELL_MS):
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
    def __init__(self, inner, artifacts, cell, *, disclose=True, repair_budget=None):
        self.inner, self.artifacts, self.cell, self.disclose = inner, artifacts, cell, disclose
        self.repair_budget = repair_budget
        self.name = inner.name
        self.requests, self.failure = 0, None
        self.traces = []

    def propose(self, request):
        if self.failure:
            raise BudgetStop("provider_already_failed")
        query = copy.deepcopy(request)
        if self.disclose and query["purpose"] == "choose_action":
            query["context"].append(dict(ref=query["context"][0]["ref"], origin="observation",
                                         layer="state", text=DISCLOSURE))
        elif self.disclose and query["purpose"] == "appraise":
            if len(query["context"]) != 1:
                raise ProviderError("Appraisal requires exactly one context reference")
            payload = json.loads(query["context"][0]["text"])
            if not isinstance(payload, dict) or not {"instructions", "packet"} <= payload.keys():
                raise ProviderError("Appraisal requires the instructions/packet envelope")
            payload["experiment_metadata"] = dict(scripted_past=True, prior_choices_not_autonomous=True)
            query["context"][0]["text"] = json.dumps(payload, ensure_ascii=False)
        self.requests += 1
        name = f"{self.cell}/requests/{self.requests:03d}.json"
        trace = dict(request=query, response=None, error=None)
        self.traces.append(trace)
        self.artifacts.write(name, trace)
        try:
            if self.repair_budget is not None and query["purpose"] == "choose_action" and query.get("limits", {}).get("attempt", 0) > 0:
                self.repair_budget.consume("cycle")
            result = self.inner.propose(query)
        except Exception as exc:
            self.failure = self.artifacts.clean(dict(error_class=type(exc).__name__, diagnostic=str(exc)))
            trace["error"] = self.failure
            trace.update(format_codes=self.format_codes())
            if self.repair_budget is not None:
                trace["shared_format_budget"] = self.repair_budget.snapshot()
            self.artifacts.write(name, trace)
            # Cycle also journals exceptions; prevent raw transport diagnostics leaking there.
            exc.args = (self.failure["diagnostic"],)
            if str(exc) != self.failure["diagnostic"]:
                # OSError/custom exceptions can include attributes beyond args in __str__.
                raise ProviderError(self.failure["error_class"] + ": " + self.failure["diagnostic"]) from None
            raise
        trace["response"] = result
        trace["format_codes"] = self.format_codes()
        if self.repair_budget is not None:
            trace["shared_format_budget"] = self.repair_budget.snapshot()
        self.artifacts.write(name, trace)
        if self.artifacts.find(json.dumps(result, ensure_ascii=False)):
            self.failure = dict(error_class="SecretInResponse", diagnostic="Provider response contained a secret; sanitized trace retained")
            trace["error"] = self.failure
            self.artifacts.write(name, trace)
            raise RuntimeError(self.failure["diagnostic"])
        return result

    def format_codes(self):
        allowed = {"stream_jsonl", "final_metadata", "final_count", "final_json", "final_object",
                   "unclassified_format", "proposal_schema"}
        values = getattr(self.inner, "last_format_codes", [])
        if not isinstance(values, list):
            return ["unsupported_format_code"] if values else []
        result = [item if isinstance(item, str) and item in allowed else "unsupported_format_code" for item in values[:2]]
        if len(values) > 2:
            result.append("unsupported_format_code")
        return result


def valid_choice_trace(trace):
    request, response = trace.get("request", {}), trace.get("response")
    return (request.get("purpose") == "choose_action" and trace.get("error") is None and isinstance(response, dict)
            and not jsonschema.errors("response", response) and bool(response.get("proposals")) and response.get("status") == "ok"
            and response.get("tool_use_detected") is False and not tool_use_signs(response)
            and all(response.get(k) == request.get(k) for k in ("request_id", "epoch", "state_version")))


def local_format_rejection(failure, appraisal_errors, traces, events, session_running):
    """D064 final flag only. All unknown/infra/budget/tool/state failures remain fatal."""
    if not session_running or any(isinstance(t.get("response"), dict) and
                                  (t["response"].get("tool_use_detected") is True or tool_use_signs(t["response"])) for t in traces):
        return False
    if any(e["type"] == "system.error" and e["payload"].get("fault") != "provider_error" for e in events):
        return False
    if failure is not None:
        codes = traces[-1].get("format_codes", []) if traces else []
        return (failure.get("error_class") == "ProviderFormatError" and bool(codes)
                and all(code in {"final_json", "final_object", "proposal_schema"} for code in codes)
                and all(error["error"] in {"ProviderFormatError", "assessment_format_or_cites"} for error in appraisal_errors))
    return bool(appraisal_errors) and all(error["error"] == "assessment_format_or_cites" for error in appraisal_errors)


def decision_technical_faults(report, events, traces):
    """Match only a proven Cycle format repair; retain every other technical signal."""
    repaired_keys, previous = {}, {}
    for trace in traces:
        query, response = trace.get("request", {}), trace.get("response")
        prior = previous.get(query.get("retry_of"))
        if (report["outcome"] not in ("no_decision", "failed", "skipped")
                and query.get("purpose") == "choose_action" and query.get("limits", {}).get("attempt") == 1
                and trace.get("error") is None and prior is not None and prior.get("error") is None
                and prior["request"].get("purpose") == "choose_action" and prior.get("response") is not None
                and isinstance(response, dict) and not jsonschema.errors("response", response)
                and response.get("status") == "ok" and response.get("tool_use_detected") is False
                and not tool_use_signs(response)
                and all(response.get(k) == query.get(k) for k in ("request_id", "epoch", "state_version"))):
            errors = jsonschema.errors("response", prior["response"])
            if errors and not tool_use_signs(prior["response"]):
                try:
                    fingerprint = hashlib.sha256(canonical_json(prior["response"]).encode()).hexdigest()[:16]
                except (TypeError, ValueError):
                    pass
                else:
                    repaired_keys["err:bad_response:" + fingerprint] = dict(errors=errors,
                         failed_request_id=prior["request"]["request_id"], repair_request_id=query["request_id"])
        previous[query.get("request_id")] = trace
    faults, resolved, classification_events = [], [], []
    for event in events:
        repair = repaired_keys.get(event.get("idempotency_key"))
        if (event["type"] == "system.error" and event["payload"].get("fault") == "bad_response"
                and repair is not None and event["payload"].get("errors") == repair["errors"]):
            resolved.append(dict(event=event, repair=repair))
            continue
        classification_events.append(event)
        if event["type"] == "system.error":
            faults.append(dict(source="event", event=event))
    failed_stages = [stage for stage in report["stages"] if stage["stage"] in ("action", "confirmation", "memory_update")
                     and stage["status"] == "failed"]
    faults += [dict(source="stage", stage=stage) for stage in failed_stages]
    return faults, resolved, classification_events, failed_stages


def planned_cell_call_limit(session, entry, tool_failure, was_running, limit_events):
    """Only our post-receipt calls==cap pause is a normal bounded cell ending."""
    usage = session.usage()
    pauses = [e for e in limit_events if e["type"] == "session.paused"]
    return (was_running and session.ctl.session_state == "paused"
            and session.ctl.state["world_time"].get("pause_reason") == "session_limit"
            and len(pauses) == 1 and pauses[0]["payload"].get("reason") == "session_limit"
            and pauses[0]["payload"].get("session_id") == session.ctl.session_id
            and entry["valid_typed_choice"] and entry["report"]["outcome"] in ("done", "rejected", "refused")
            and entry["provider_error"] is None and not entry["assessment_errors"] and not tool_failure
            and not entry["technical_faults"] and not any(e["type"] == "system.error" for e in limit_events)
            and type(usage["max_calls"]) is int and 1 <= usage["max_calls"] <= CELL_CALLS
            and usage["calls"] == usage["max_calls"]
            and type(usage["max_ms"]) is int and 0 < usage["max_ms"] <= CELL_MS and 0 <= usage["elapsed_ms"] < usage["max_ms"])


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


def require(condition, reason):
    if not condition:
        raise ValueError("Repair refused: " + reason)


def safe_original_file(root, relative):
    """Only fixed relative evidence paths; never follow a source symlink."""
    relative = Path(relative)
    require(not relative.is_absolute() and ".." not in relative.parts, "unsafe artifact path")
    target = root
    for part in relative.parts:
        target /= part
        require(not target.is_symlink(), "symlink in original evidence")
    require(target.is_file() and target.resolve().is_relative_to(root), "missing original artifact")
    require(target.stat().st_size <= 64 * 1024 * 1024, "oversized original artifact")
    return target


def immutable_persona(path):
    # immutable=1 prevents SQLite from creating WAL/SHM sidecars beside OLD evidence.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as con:
        meta = dict(con.execute("SELECT key,value FROM meta"))
    require(meta.get("store_kind") == "experimental" and meta.get("persona_id", "").startswith("exp_"), "original database must be experimental")
    return meta["persona_id"]


def load_repair(original, output, artifacts):
    try:
        return _load_repair(original, output, artifacts)
    except (OSError, sqlite3.Error, KeyError, TypeError, AttributeError):
        raise ValueError("Repair refused: invalid original evidence") from None


def _load_repair(original, output, artifacts):
    """Validate a single failed original lineage and inventory its immutable evidence."""
    supplied = Path(original).expanduser().absolute()
    require(supplied.is_dir() and not supplied.is_symlink(), "original must be a real directory")
    root = supplied.resolve()
    destination = output.resolve()
    require(not destination.is_relative_to(root) and not root.is_relative_to(destination), "output and original must be disjoint")
    inventory = {}

    def read(relative, *, json_data=True):
        source = safe_original_file(root, relative)
        inventory[relative] = sha(source)
        if not json_data:
            return source
        value = json.loads(source.read_text(encoding="utf-8"))
        require(not artifacts.find(json.dumps(value, ensure_ascii=False)), "original JSON needs redaction")
        return value

    manifest, summary, calls = (read(name) for name in ("manifest.json", "summary.json", "calls.json"))
    require(not any(key in manifest or key in summary for key in ("repair_of", "repair_count")), "second correction is forbidden")
    require(manifest.get("mode") == summary.get("mode") == "live", "original must be live")
    require(summary.get("status") == "partial" and summary.get("stop") == "assessment_error", "original did not stop on an appraisal error")
    require(manifest.get("model") == MODEL and manifest.get("plan") == plan(), "original plan/model differs")
    require(manifest.get("preparation_disclosure") == DISCLOSURE and manifest.get("question") == QUESTION, "free-choice framing changed")
    require(summary.get("manifest_sha256") == inventory["manifest.json"], "original manifest hash mismatch")
    expected_budgets = dict(global_decisions=MAX_DECISIONS, actual_model_calls=MAX_CALLS,
                            cell_model_calls=CELL_CALLS, cell_ms=CELL_MS, forecast_calls=FORECAST,
                            provider_format_retries=0, cycle_format_retries=1, cycle_stale_retries=0)
    require(manifest.get("budgets") == expected_budgets, "original budgets differ")
    require(isinstance(manifest.get("sources"), dict) and bool(manifest["sources"]), "missing original source hashes")
    require(all(isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v)
                for k, v in manifest["sources"].items()), "invalid source hashes")
    attempts, cells = calls.get("attempts"), summary.get("cells")
    require(isinstance(attempts, list) and isinstance(cells, list) and 0 < len(cells) <= len(plan()), "invalid original history")
    for key, maximum in (("actual_model_calls", MAX_CALLS), ("decisions_started", MAX_DECISIONS)):
        require(type(summary.get(key)) is int and 0 <= summary[key] <= maximum
                and type(calls.get(key)) is int and summary[key] == calls[key], "inconsistent original counters")
    require(len(attempts) == summary["actual_model_calls"], "actual-call ledger mismatch")
    require(type(summary.get("typed_requests")) is int and summary["typed_requests"] == len(attempts), "typed-call ledger mismatch")
    require(MAX_CALLS - len(attempts) >= FORECAST and summary["decisions_started"] < MAX_DECISIONS, "no remaining global budget")
    require(all(isinstance(row, dict) and type(row.get("index")) is int and row["index"] == index and row.get("status") == "returned"
                and row.get("returncode") == 0 and row.get("timed_out") is False
                for index, row in enumerate(attempts, 1)), "unfinished or inconsistent original call")

    fixtures = {}
    require(set(manifest.get("baselines", {})) == set(FAMILIES), "baseline family mismatch")
    for family in FAMILIES:
        fixture = manifest["baselines"][family]
        relative = f"baselines/{family}/baseline.db"
        require(isinstance(fixture, dict) and fixture.get("db") == relative and fixture.get("scripted_past") is True,
                "unsafe or unprepared baseline")
        db = read(relative, json_data=False)
        require(inventory[relative] == fixture.get("sha256"), "baseline hash mismatch")
        persona_id = immutable_persona(db)
        fixture_relative = f"baselines/{family}/fixture.json"
        fixture_data = read(fixture_relative)
        require(fixture_data.get("family") == family and fixture_data.get("scripted_past") is True
                and fixture_data.get("autonomous_choices") is False, "fixture disclosure mismatch")
        prep_dir = root / f"baselines/{family}/preparation/requests"
        if prep_dir.exists() or prep_dir.is_symlink():
            require(prep_dir.is_dir() and not prep_dir.is_symlink(), "unsafe preparation directory")
            preparation_files = sorted(prep_dir.iterdir())
            require(len(preparation_files) <= 10, "too many preparation artifacts")
            for index, source in enumerate(preparation_files, 1):
                require(source.name == f"{index:03d}.json", "unexpected preparation artifact")
                read(str(source.relative_to(root)))
        fixtures[family] = dict(fixture, fixture_sha256=inventory[fixture_relative], experimental_persona_id=persona_id)

    total_decisions, total_typed, trace_ids = 0, 0, []
    for index, cell in enumerate(cells):
        expected = plan()[index]
        require(isinstance(cell, dict) and all(cell.get(k) == v for k, v in expected.items()), "original cells are not a sequential prefix")
        name = expected["cell"]
        require(cell.get("baseline_sha256") == fixtures[expected["family"]]["sha256"], "cell baseline mismatch")
        decisions = cell.get("decisions")
        require(isinstance(decisions, list) and 0 < len(decisions) <= expected["max_decisions"], "invalid cell decisions")
        is_failed = index == len(cells) - 1
        if is_failed:
            require(cell.get("technical_status") == "stopped" and cell.get("stop") == "assessment_error"
                    and cell.get("error", {}).get("error_class") == "AssessmentError", "last cell must be the failed appraisal")
            require(len(decisions) < expected["max_decisions"], "failed cell has no decision allowance left")
        else:
            require(cell.get("technical_status") == "completed" and cell.get("stop") is None
                    and cell.get("error") is None and len(decisions) == expected["max_decisions"], "prefix cell is incomplete")
        usage = cell.get("session_usage", {})
        require(usage.get("max_calls") == CELL_CALLS and usage.get("max_ms") == CELL_MS
                and type(usage.get("calls")) is int and 0 < usage["calls"] <= CELL_CALLS
                and type(usage.get("elapsed_ms")) is int and 0 <= usage["elapsed_ms"] < CELL_MS,
                "original session budget mismatch")
        final = read(f"cells/{name}/final.json")
        require(final.get("record") == cell, "final cell record differs from summary")
        cell_db = read(f"cells/{name}/persona.db", json_data=False)
        require(immutable_persona(cell_db) == fixtures[expected["family"]]["experimental_persona_id"], "cell is not a branch of its baseline persona")
        typed_in_cell = 0
        for number, decision in enumerate(decisions, 1):
            require(decision.get("index") == number, "decision numbering mismatch")
            entry = read(f"cells/{name}/decisions/{number:02d}.json")
            require(entry.get("index") == number and entry.get("report", {}).get("outcome") == decision.get("outcome"), "decision evidence mismatch")
            errors = assessment_errors(entry.get("events", []))
            require(entry.get("assessment_errors") == decision.get("assessment_errors") == errors, "appraisal diagnostics mismatch")
            require(type(entry.get("typed_requests")) is int and entry["typed_requests"] > 0, "invalid typed-request count")
            typed_in_cell += entry["typed_requests"]
            if is_failed and number == len(decisions):
                require(bool(errors) and all(row["error"] == "assessment_format_or_cites" for row in errors), "uncorrectable appraisal failure")
                require(cell["error"].get("assessments") == errors and entry.get("provider_error") is None,
                        "failed cell has other provider errors")
            else:
                require(not errors and entry.get("provider_error") is None
                        and decision.get("outcome") in ("done", "rejected", "refused"), "prefix has a technical failure")
        require(typed_in_cell == usage["calls"], "session/request counter mismatch")
        request_dir = root / f"cells/{name}/requests"
        require(request_dir.is_dir() and not request_dir.is_symlink(), "unsafe request directory")
        require({p.name for p in request_dir.iterdir()} == {f"{n:03d}.json" for n in range(1, typed_in_cell + 1)}, "extra/missing typed requests")
        for number in range(1, typed_in_cell + 1):
            trace = read(f"cells/{name}/requests/{number:03d}.json")
            request, response = trace.get("request", {}), trace.get("response", {})
            require(trace.get("error") is None and isinstance(response, dict)
                    and request.get("request_id") == response.get("request_id"), "incomplete original typed response")
            require(not jsonschema.errors("request", request) and not jsonschema.errors("response", response), "invalid original typed request/response")
            require(request.get("purpose") in ("choose_action", "appraise"), "unexpected typed purpose")
            if not is_failed:
                require(request["purpose"] == "choose_action" and request.get("limits", {}).get("attempt") == 0,
                        "completed prefix contains an old appraisal or repair")
                context = request.get("context", [])
                require(bool(context) and context[-1] == dict(ref=context[0]["ref"], origin="observation", layer="state", text=DISCLOSURE)
                        and any(item.get("text") == GUIDANCE for item in context), "prefix free-choice guidance differs")
            trace_ids.append((name, request.get("request_id")))
        total_typed += typed_in_cell
        total_decisions += len(decisions)
    require(total_decisions == summary["decisions_started"] and total_typed == summary["typed_requests"], "history total mismatch")
    require(trace_ids == [(row.get("cell"), row.get("request_id")) for row in attempts], "call/request ordering mismatch")
    require(len({rid for _, rid in trace_ids}) == len(trace_ids), "duplicate original request")
    failed = cells[-1]
    inherited_failed = len(failed["decisions"])
    remaining_calls = CELL_CALLS - failed["session_usage"]["calls"]
    remaining_ms = CELL_MS - failed["session_usage"]["elapsed_ms"]
    require(remaining_calls >= FORECAST and remaining_ms > 0, "failed logical cell has no remaining session budget")
    adjusted = []
    for index, cell in enumerate(plan()):
        reused = index < len(cells) - 1
        spent = len(cells[index]["decisions"]) if index < len(cells) else 0
        adjusted.append(dict(cell, reuse_completed=reused, inherited_decisions=spent,
                             new_max_decisions=0 if reused else cell["max_decisions"] - spent,
                             restart_from_baseline=index == len(cells) - 1,
                             new_max_calls=remaining_calls if index == len(cells) - 1 else CELL_CALLS,
                             new_max_ms=remaining_ms if index == len(cells) - 1 else CELL_MS))
    require(sum(c["new_max_decisions"] for c in adjusted) == MAX_DECISIONS - total_decisions, "adjusted decision budget mismatch")
    return dict(root=root, manifest=manifest, summary=summary, calls=calls, inventory=inventory,
                baselines=fixtures, plan=adjusted, inherited_failed_decisions=inherited_failed,
                prefix=copy.deepcopy(cells[:-1]), failed_cell=failed["cell"])


def source_changes(before, after, permitted):
    require(isinstance(before, dict) and isinstance(after, dict) and set(before) == set(after), "source inventory changed")
    require(all(isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v)
                for k, v in after.items()), "invalid source hashes")
    changed = {name for name in after if before[name] != after[name]}
    require(changed <= permitted, "sources changed beyond the authorized correction")
    return changed


def load_final_repair(original, output, artifacts):
    try:
        return _load_final_repair(original, output, artifacts)
    except (OSError, sqlite3.Error, KeyError, TypeError, AttributeError):
        raise ValueError("Final repair refused: invalid original evidence") from None


def _load_final_repair(original, output, artifacts):
    supplied = Path(original).expanduser().absolute()
    require(supplied.is_dir() and not supplied.is_symlink(), "final source must be a real directory")
    root = supplied.resolve()
    require(not output.resolve().is_relative_to(root) and not root.is_relative_to(output.resolve()), "final output and original must be disjoint")
    inventory = {}

    def read(relative, *, json_data=True):
        path = safe_original_file(root, relative)
        inventory[relative] = sha(path)
        if not json_data:
            return path
        value = json.loads(path.read_text(encoding="utf-8"))
        require(not artifacts.find(json.dumps(value, ensure_ascii=False)), "final source JSON needs redaction")
        return value

    manifest, summary, calls = (read(name) for name in ("manifest.json", "summary.json", "calls.json"))
    require(manifest.get("repair_count") == summary.get("repair_count") == 1
            and "final_repair_of" not in manifest and "final_repair_of" not in summary, "further final correction is forbidden")
    require(manifest.get("version") == summary.get("version") == "s22-series-2"
            and manifest.get("mode") == summary.get("mode") == "live", "final source is not live version2")
    require(summary.get("status") == "partial" and summary.get("stop") == "provider_or_cycle_failure", "wrong final-source stop")
    require(summary.get("manifest_sha256") == inventory["manifest.json"], "version2 manifest hash mismatch")
    require(manifest.get("model") == MODEL and manifest.get("budgets") == budgets(native_retries=0)
            and manifest.get("preparation_disclosure") == DISCLOSURE and manifest.get("question") == QUESTION,
            "version2 model/budgets/framing differs")

    # Validate the genuine transitive live1 copy. Never fabricate a version1 manifest from live2.
    ancestor = load_repair(root / "original", output, artifacts)
    require(ancestor["manifest"].get("version") == ancestor["summary"].get("version") == "s22-series-1", "wrong original source version")
    require((ancestor["summary"]["decisions_started"], ancestor["summary"]["actual_model_calls"],
             len(ancestor["prefix"]), ancestor["failed_cell"], ancestor["inherited_failed_decisions"])
            == (18, 19, 4, "return_unfinished-main-1", 2), "unexpected live1 branch")
    require(len(ancestor["inventory"]) == 78, "unexpected live1 artifact inventory")
    old_ref = manifest.get("repair_of", {})
    require(isinstance(old_ref, dict) and old_ref.get("artifact_sha256") == ancestor["inventory"], "transitive artifact hashes differ")
    original_directory = Path(old_ref.get("directory", ""))
    require(original_directory.is_absolute() and original_directory != Path("/") and ".." not in original_directory.parts,
            "unsafe transitive evidence directory")
    require(not output.resolve().is_relative_to(original_directory.resolve())
            and not original_directory.resolve().is_relative_to(output.resolve()), "output overlaps transitive evidence")
    require(summary.get("repair_of") == str(original_directory), "lineage directory mismatch")
    old_allowed = {"runtime/intentions/completion.py", "scripts/world/series.py", "scripts/world/README.md"}
    changed = source_changes(ancestor["manifest"]["sources"], manifest.get("sources"), old_allowed)
    expected_ref = dict(directory=str(original_directory), manifest_sha256=ancestor["inventory"]["manifest.json"],
                        summary_sha256=ancestor["inventory"]["summary.json"], calls_sha256=ancestor["inventory"]["calls.json"],
                        artifact_sha256=ancestor["inventory"], changed_sources=sorted(changed),
                        unchanged_source_sha256={k: v for k, v in manifest["sources"].items() if k not in old_allowed},
                        free_choice_disclosure_unchanged=True, world_guidance_source_unchanged=True,
                        disclosure_sha256=hashlib.sha256(DISCLOSURE.encode()).hexdigest(),
                        guidance_sha256=hashlib.sha256(GUIDANCE.encode()).hexdigest(),
                        guidance_source_sha256=manifest["sources"]["runtime/world/cycle.py"], prefix_had_no_appraisals=True)
    require(old_ref == expected_ref, "version1-to-version2 provenance mismatch")
    for relative, digest in ancestor["inventory"].items():
        read("original/" + relative, json_data=False)
        require(inventory["original/" + relative] == digest, "changed transitive evidence")
    require(manifest.get("plan") == ancestor["plan"] and manifest.get("original_plan") == plan(), "version2 adjusted plan differs")
    require(manifest.get("baselines") == ancestor["baselines"], "version2 baseline metadata differs")
    for family in FAMILIES:
        for filename in ("baseline.db", "fixture.json"):
            relative = f"baselines/{family}/{filename}"
            read(relative, json_data=filename.endswith("json"))
            require(inventory[relative] == ancestor["inventory"][relative], "version2 baseline/fixture bytes differ")
    inherited = dict(inherited_decisions_started=18, inherited_model_calls=19,
                     inherited_typed_requests=19, inherited_failed_decisions=2)
    require(all(manifest.get(k) == summary.get(k) == v for k, v in inherited.items()), "version2 inherited counters differ")
    require(manifest.get("new_decisions_max") == 42 and manifest.get("selected_logical_decisions_max") == 58,
            "version2 maximum accounting differs")
    expected_history = dict(cell=ancestor["failed_cell"], decisions=ancestor["summary"]["cells"][-1]["decisions"],
                            evidence="original/cells/" + ancestor["failed_cell"])
    require(summary.get("inherited_failed_history") == expected_history, "old failed history changed")
    require(all(type(summary.get(k)) is int and summary[k] == value for k, value in
                dict(decisions_started=22, actual_model_calls=23, typed_requests=23, new_decisions_started=4,
                     new_actual_model_calls=4, new_typed_requests=4, selected_logical_decisions=20).items()), "version2 counters differ")
    attempts = calls.get("attempts")
    require(calls.get("decisions_started") == 22 and calls.get("actual_model_calls") == 23
            and isinstance(attempts, list) and len(attempts) == 23 and attempts[:19] == ancestor["calls"]["attempts"],
            "version2 ledger inheritance differs")
    require(all(row.get("index") == index and row.get("status") == "returned" and row.get("returncode") == 0
                and row.get("timed_out") is False for index, row in enumerate(attempts, 1)), "version2 transport failure/ordering")
    cells = summary.get("cells")
    require(isinstance(cells, list) and len(cells) == 6, "version2 must contain five completed and one failed cell")
    prefix_ref = dict(directory=str(original_directory), manifest_sha256=ancestor["inventory"]["manifest.json"])
    for index, cell in enumerate(cells[:4]):
        stripped = dict(cell)
        require(stripped.pop("reused_from", None) == prefix_ref and stripped == ancestor["summary"]["cells"][index],
                "reused version1 cell metadata changed")
        prefix = f"cells/{cell['cell']}/"
        for relative, digest in ancestor["inventory"].items():
            if relative.startswith(prefix):
                value = read(relative, json_data=relative.endswith(".json"))
                require(inventory[relative] == digest, "reused prefix artifact changed")
                if "/requests/" in relative:
                    require(valid_choice_trace(value) and value["request"]["limits"].get("attempt") == 0
                            and value["response"].get("usage", {}).get("calls") == 1, "reused original prefix has a retry/appraisal")
        final = read(prefix + "final.json")
        require(final.get("record") == stripped, "reused final record mismatch")

    new_ids = []
    for index in (4, 5):
        cell, expected = cells[index], ancestor["plan"][index]
        require(isinstance(cell, dict) and all(cell.get(k) == v for k, v in expected.items()), "version2 new cell plan mismatch")
        name = cell["cell"]
        require(cell.get("baseline_sha256") == ancestor["baselines"][cell["family"]]["sha256"], "version2 cell baseline mismatch")
        require(len(cell.get("decisions", [])) == 2, "version2 new cell must have two decisions")
        if index == 4:
            require(cell.get("technical_status") == "completed" and cell.get("stop") is None and cell.get("error") is None
                    and cell.get("inherited_session_usage") == ancestor["summary"]["cells"][-1]["session_usage"], "first corrected cell is incomplete")
        else:
            require(cell.get("technical_status") == "stopped" and cell.get("stop") == "provider_or_cycle_failure"
                    and cell.get("error", {}).get("error_class") == "ProviderFormatError", "final source failure is not ProviderFormatError")
        usage = cell.get("session_usage", {})
        require(usage.get("calls") == 2 and usage.get("max_calls") == expected["new_max_calls"]
                and usage.get("max_ms") == expected["new_max_ms"] and type(usage.get("elapsed_ms")) is int
                and 0 <= usage["elapsed_ms"] < usage["max_ms"], "version2 session usage differs")
        require(read(f"cells/{name}/final.json").get("record") == cell, "version2 final record mismatch")
        db = read(f"cells/{name}/persona.db", json_data=False)
        require(immutable_persona(db) == ancestor["baselines"][cell["family"]]["experimental_persona_id"], "version2 persona mismatch")
        request_dir = root / f"cells/{name}/requests"
        require(request_dir.is_dir() and not request_dir.is_symlink() and {p.name for p in request_dir.iterdir()} == {"001.json", "002.json"},
                "unexpected version2 typed requests")
        for number in (1, 2):
            trace = read(f"cells/{name}/requests/{number:03d}.json")
            entry = read(f"cells/{name}/decisions/{number:02d}.json")
            decision = cell["decisions"][number - 1]
            request, response = trace.get("request", {}), trace.get("response")
            require(not jsonschema.errors("request", request) and request.get("purpose") == "choose_action"
                    and request.get("limits", {}).get("attempt") == 0, "version2 prefix contains appraisal/format repair")
            context = request["context"]
            require(context[-1] == dict(ref=context[0]["ref"], origin="observation", layer="state", text=DISCLOSURE)
                    and any(item.get("text") == GUIDANCE for item in context), "version2 free guidance differs")
            require(entry.get("index") == decision.get("index") == number and entry.get("typed_requests") == 1
                    and entry.get("physical_decision_index") == 19 + (index - 4) * 2 + number - 1
                    and not assessment_errors(entry.get("events", [])) and entry.get("assessment_errors") == decision.get("assessment_errors") == [],
                    "version2 decision diagnostics/counters differ")
            report = entry.get("report", {})
            require(report.get("requests") == [request["request_id"]] and report.get("outcome") == decision.get("outcome")
                    and report.get("reason") == decision.get("reason") and report.get("actions") == decision.get("actions"), "version2 report differs")
            requested = [event for event in entry["events"] if event["type"] == "proposal.requested"]
            kernel_query = copy.deepcopy(request)
            kernel_query["context"].pop()
            require(len(requested) == 1 and requested[0]["payload"].get("request") == kernel_query, "version2 request event differs")
            if index == 5 and number == 2:
                error = trace.get("error")
                require(response is None and isinstance(error, dict) and error.get("error_class") == "ProviderFormatError"
                        and entry.get("provider_error") == cell.get("error") == error, "final typed failure differs")
                require(report.get("outcome") == "no_decision" and report.get("reason") == "provider_error"
                        and any(stage.get("stage") == "proposal" and stage.get("status") == "failed"
                                and stage.get("detail", {}).get("error") == "ProviderFormatError" for stage in report.get("stages", [])), "failed report is inconsistent")
                faults = [e for e in entry["events"] if e["type"] == "system.error" and e["payload"].get("fault") == "provider_error"]
                require(len(faults) == 1 and faults[0]["payload"].get("request_id") == request["request_id"]
                        and faults[0]["payload"].get("error") == "ProviderFormatError: " + error["diagnostic"], "failed event is inconsistent")
            else:
                require(valid_choice_trace(trace) and entry.get("provider_error") is None and isinstance(response, dict)
                        and not jsonschema.errors("response", response) and response.get("request_id") == request["request_id"]
                        and response.get("usage", {}).get("calls") == 1 and report.get("outcome") in ("done", "rejected", "refused"), "invalid reused choice response")
            new_ids.append((name, request["request_id"]))
    require(new_ids == [(r.get("cell"), r.get("request_id")) for r in attempts[19:]], "version2 new call IDs differ")
    require(len({r["request_id"] for r in attempts}) == 23, "version2 duplicate actual request")
    failed = cells[-1]
    adjusted = []
    for index, cell in enumerate(plan()):
        spent = len(cells[index]["decisions"]) if index < len(cells) else 0
        adjusted.append(dict(cell, reuse_completed=index < 5, inherited_decisions=spent,
                             new_max_decisions=0 if index < 5 else cell["max_decisions"] - spent,
                             restart_from_baseline=index == 5,
                             new_max_calls=CELL_CALLS - failed["session_usage"]["calls"] if index == 5 else CELL_CALLS,
                             new_max_ms=CELL_MS - failed["session_usage"]["elapsed_ms"] if index == 5 else CELL_MS))
    require(sum(c["new_max_decisions"] for c in adjusted) == 38 and adjusted[5]["new_max_calls"] >= FORECAST
            and adjusted[5]["new_max_ms"] > 0, "no final correction allowance")
    histories = [dict(source_version=ancestor["manifest"]["version"], manifest_sha256=ancestor["inventory"]["manifest.json"],
                      cell=ancestor["failed_cell"], decisions=copy.deepcopy(expected_history["decisions"]),
                      evidence="original/original/cells/" + ancestor["failed_cell"]),
                 dict(source_version=manifest["version"], manifest_sha256=inventory["manifest.json"],
                      cell=failed["cell"], decisions=copy.deepcopy(failed["decisions"]), evidence="original/cells/" + failed["cell"])]
    return dict(root=root, manifest=manifest, summary=summary, calls=calls, inventory=inventory,
                baselines=ancestor["baselines"], plan=adjusted, inherited_failed_decisions=4,
                prefix=copy.deepcopy(cells[:5]), failed_cell=failed["cell"], repair_count=2,
                ancestor=ancestor, inherited_failed_histories=histories,
                evidence_roots=[root, original_directory.resolve()])


def preserve_repair(repair, output, frozen):
    old_sources = repair["manifest"]["sources"]
    final = repair.get("repair_count") == 2
    permitted = {"runtime/providers/codex/provider.py" if final else "runtime/intentions/completion.py",
                 "scripts/world/series.py", "scripts/world/README.md"}
    changed = source_changes(old_sources, frozen, permitted)
    for relative, digest in repair["inventory"].items():
        source = safe_original_file(repair["root"], relative)
        require(sha(source) == digest, "original changed while validating")
        destination = output / "original" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        require(sha(destination) == digest, "original copy mismatch")
    for family in FAMILIES:
        for name in ("baseline.db", "fixture.json"):
            relative = f"baselines/{family}/{name}"
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output / "original" / relative, destination)
    for cell in repair["prefix"]:
        prefix = f"cells/{cell['cell']}/"
        for relative in repair["inventory"]:
            if relative.startswith(prefix):
                destination = output / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(output / "original" / relative, destination)
        reuse = dict(directory=str(repair["root"]), manifest_sha256=repair["inventory"]["manifest.json"])
        if final:
            cell["reused_in_final_from"] = reuse
            cell["source_version"] = "s22-series-1" if "reused_from" in cell else repair["manifest"]["version"]
            cell["valid_typed_choice_decisions"] = len(cell["decisions"])
        else:
            cell["reused_from"] = reuse
    lineage = dict(directory=str(repair["root"]), manifest_sha256=repair["inventory"]["manifest.json"],
                summary_sha256=repair["inventory"]["summary.json"], calls_sha256=repair["inventory"]["calls.json"],
                artifact_sha256=repair["inventory"], changed_sources=sorted(changed),
                unchanged_source_sha256={k: v for k, v in frozen.items() if k not in permitted},
                free_choice_disclosure_unchanged=True, world_guidance_source_unchanged=True,
                disclosure_sha256=hashlib.sha256(DISCLOSURE.encode()).hexdigest(),
                guidance_sha256=hashlib.sha256(GUIDANCE.encode()).hexdigest(),
                guidance_source_sha256=frozen["runtime/world/cycle.py"],
                prefix_had_no_appraisals=True)
    if final:
        ancestor = repair["ancestor"]
        lineage.update(previous=copy.deepcopy(repair["manifest"]["repair_of"]), authorization="D064",
                       source_versions=[dict(version=ancestor["manifest"]["version"], manifest_sha256=ancestor["inventory"]["manifest.json"],
                                             sources=ancestor["manifest"]["sources"]),
                                        dict(version=repair["manifest"]["version"], manifest_sha256=repair["inventory"]["manifest.json"],
                                             sources=repair["manifest"]["sources"])])
    return lineage


def run_series(output, *, mode="stub", auth_file=None, provider_factory=None, scanner=None, repair_from=None, final_repair_from=None):
    if mode not in ("stub", "live"):
        raise ValueError("Unknown mode")
    if mode == "live" and not auth_file:
        raise ValueError("Live mode requires an external --auth-file")
    if repair_from is not None and final_repair_from is not None:
        raise ValueError("Correction flags are mutually exclusive")
    correction_source = final_repair_from if final_repair_from is not None else repair_from
    if correction_source is not None and mode != "live":
        raise ValueError("Correction requires live mode; tests must mock the provider/transport")
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output must be a NEW directory; resume/overwrite is forbidden")
    private_paths = ()
    if auth_file is not None:
        auth = Path(auth_file).expanduser().absolute()
        if auth == output or output in auth.parents:
            raise ValueError("Credentials must be external to the output directory")
        if correction_source is not None:
            original_root = Path(correction_source).expanduser().absolute().resolve()
            require(not auth.is_relative_to(original_root) and not auth.resolve().is_relative_to(original_root),
                    "credentials must be external to the immutable original")
        private_paths = tuple({path for path in (str(auth_file), str(auth), str(auth.resolve())) if len(path) >= 4})
    artifacts = Artifacts(output, scanner, private_paths=private_paths)
    repair = (load_final_repair(final_repair_from, output, artifacts) if final_repair_from is not None else
              load_repair(repair_from, output, artifacts) if repair_from is not None else None)
    if repair:
        for evidence_root in repair.get("evidence_roots", []):
            require(not auth.is_relative_to(evidence_root) and not auth.resolve().is_relative_to(evidence_root),
                    "credentials must be external to all original evidence")
    output.mkdir(parents=True, mode=0o700)
    inherited_decisions = repair["summary"]["decisions_started"] if repair else 0
    inherited_typed = repair["summary"]["typed_requests"] if repair else 0
    inherited_calls = len(repair["calls"]["attempts"]) if repair else 0
    inherited_failed = repair["inherited_failed_decisions"] if repair else 0
    ledger = CallLedger(artifacts, attempts=repair["calls"]["attempts"] if repair else (), decisions=inherited_decisions)
    summary = dict(version=VERSION, mode=mode, status="preparing", stop=None, cells=[],
                   actual_model_calls=inherited_calls, decisions_started=inherited_decisions, typed_requests=inherited_typed,
                   inherited_decisions_started=inherited_decisions, inherited_model_calls=inherited_calls,
                   inherited_typed_requests=inherited_typed, inherited_failed_decisions=inherited_failed,
                   scripted_past=True, behavioral_success=None,
                   interpretation="Stub is transport/mechanism evidence only. No new goal/rest/negative behavior is a valid outcome; no reruns to obtain a preferred answer.")
    if repair:
        summary.update(repair_count=repair.get("repair_count", 1), repair_of=str(repair["root"]),
                       inherited_failed_history=dict(cell=repair["failed_cell"], decisions=copy.deepcopy(repair["summary"]["cells"][-1]["decisions"]),
                                                     evidence="original/cells/" + repair["failed_cell"]),
                       interpretation="One authorized protocol correction. Physical counters include every original attempt. selected_logical_decisions selects the completed prefix and new branch; it is not an accepted-proposal or successful-action count. Both original failed-cell outcomes remain in original/.")
        if final_repair_from is not None:
            summary.update(final_repair_of=str(repair["root"]), inherited_failed_histories=repair["inherited_failed_histories"],
                           local_rejections=[],
                           prior_inherited_failed_history=copy.deepcopy(repair["summary"]["inherited_failed_history"]),
                           interpretation="D064 final authorized branch. Both superseded histories and every physical call remain; selected logical decisions are not accepted proposals or successful actions.")

    def save():
        summary.update(actual_model_calls=len(ledger.attempts), decisions_started=ledger.decisions,
                       new_decisions_started=ledger.decisions - inherited_decisions,
                       new_actual_model_calls=len(ledger.attempts) - inherited_calls,
                       new_typed_requests=summary["typed_requests"] - inherited_typed,
                       selected_logical_decisions=ledger.decisions - inherited_failed)
        artifacts.write("summary.json", summary)

    save()
    try:
        frozen = source_hashes()
        if repair:
            repair_lineage = preserve_repair(repair, output, frozen)
            fixtures, execution_plan = repair["baselines"], repair["plan"]
            summary["cells"] = repair["prefix"]
            save()
        else:
            fixtures, execution_plan = {}, plan()
            for family in FAMILIES:
                scenario = tomllib.loads((ROOT / f"scenarios/motivation/mot_{family}.toml").read_text())
                if scenario["id"] != "mot_" + family:
                    raise ValueError("Unexpected scenario identity")
                db = output / f"baselines/{family}/baseline.db"
                fixtures[family] = preparation(db, family, artifacts)
        if source_hashes() != frozen:
            raise RuntimeError("Sources changed during preparation; no live calls permitted")
        manifest = dict(version=VERSION, question=QUESTION, mode=mode, model=MODEL,
                        python=platform.python_version(), sources=frozen, plan=execution_plan, baselines=fixtures,
                        budgets=budgets(), shared_format_repair_budget=1, per_decision_physical_call_cap=FORECAST,
                        preparation_disclosure=DISCLOSURE, scripted_past=True,
                        observations="Prepared histories use real local world actions driven by scripts. No initial interest/need/skill scores are fabricated. Scenario TOML expectations are historical hypotheses, never model instructions or automatic success verdicts.",
                        credentials="External auth_file passed only to the official provider; contents and path are not evidence artifacts.",
                        transport="Every actual Codex model invocation, including appraisals and repairs, is charged before subprocess_runner. Native timeout cleanup remains enabled; docker kill is technical cleanup, not a model request.")
        if repair:
            manifest.update(repair_count=repair.get("repair_count", 1), repair_of=repair_lineage, original_plan=plan(),
                            inherited_decisions_started=inherited_decisions, inherited_model_calls=inherited_calls,
                            inherited_typed_requests=inherited_typed, inherited_failed_decisions=inherited_failed,
                            new_decisions_max=sum(c["new_max_decisions"] for c in execution_plan),
                            selected_logical_decisions_max=MAX_DECISIONS - inherited_failed,
                            accounting="Physical decisions = inherited + new <=60; actual model invocations = inherited + new <=240. Selected logical decisions exclude the superseded failed-cell branch; all its real outcomes are retained.")
            if final_repair_from is not None:
                manifest["final_repair_of"] = dict(directory=str(repair["root"]), manifest_sha256=repair["inventory"]["manifest.json"], authorization="D064")
                manifest["local_rejection_policy"] = dict(provider_format_codes=["final_json", "final_object", "proposal_schema"],
                                                         assessment_errors=["assessment_format_or_cites"],
                                                         completion_requires="18 visited cells and at least one valid typed free-choice decision per cell; root reviews every rejection; no behavioral success inferred",
                                                         all_infrastructure_budget_tool_state_secret_unknown_failures="stop_all")
        artifacts.write("manifest.json", manifest, exclusive=True)
        manifest_digest = sha(output / "manifest.json")
        summary.update(status="running", manifest_sha256=manifest_digest)
        save()  # frozen manifest exists before any live provider is constructed/called
        for cell in execution_plan:
            if cell.get("reuse_completed"):
                continue
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
            if final_repair_from is not None:
                record["valid_typed_choice_decisions"] = 0
            if cell.get("restart_from_baseline"):
                record["inherited_session_usage"] = repair["summary"]["cells"][-1]["session_usage"]
            summary["cells"].append(record)
            save()
            with Store.open(db) as store:
                if store.store_kind != "experimental":
                    raise RuntimeError("Only experimental fixture DBs are permitted")
                ctl = StateController(store)
                session = SessionRunner(ctl)
                session.command("start", "EXPERIMENTAL: bounded free choice",
                                max_calls=cell.get("new_max_calls", CELL_CALLS), max_ms=cell.get("new_max_ms", CELL_MS))
                format_budget = FormatRepairBudget()
                provider_holder = {"inner": None}

                def transport(argv, stdin, timeout_s, cleanup=None):
                    format_budget.before_transport(native_repeat=getattr(provider_holder["inner"], "last_calls", 0) > 0)
                    attempt = ledger.charge(session, name)
                    save()
                    try:
                        # Do not inspect or persist stdin: official auth travels there.
                        usage = session.usage()
                        remaining_s = max(.001, (usage.get("max_ms", CELL_MS) - usage["elapsed_ms"]) / 1000)
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
                    inner = CodexProvider(transport, CodexSandboxConfig(auth_file=str(Path(auth_file).expanduser()), persist_auth_updates=True), model=MODEL, timeout_s=120, max_format_retries=NATIVE_FORMAT_RETRIES)
                provider_holder["inner"] = inner
                audited = AuditedProvider(inner, artifacts, "cells/" + name, repair_budget=format_budget)
                provider = session.provider(audited, charge=mode == "stub")
                memory = WorldMemory(ctl)
                memory.motivation_off = cell["motivation_off"]
                world = PersistentWorld(ctl)
                cycle = LivingCycle(ctl, provider, memory, world, StubCapabilities(CAPABILITIES), StubCreatorBridge(),
                                    motivation_off=cell["motivation_off"], max_rerequest=0, deadline_ms=120_000,
                                    retrieval_limit=12, max_output_tokens=1600)
                for index in range(cell.get("new_max_decisions", cell["max_decisions"])):
                    try:
                        ledger.forecast(session, live=mode == "live")
                    except BudgetStop as exc:
                        record["stop"] = str(exc)
                        usage = session.usage()
                        planned_forecast = (str(exc) == "cell_call_forecast" and ctl.session_state == "running"
                                            and type(usage["max_calls"]) is int and type(usage["max_ms"]) is int
                                            and 0 <= usage["calls"] < usage["max_calls"] <= CELL_CALLS
                                            and 0 <= usage["elapsed_ms"] < usage["max_ms"] <= CELL_MS)
                        if final_repair_from is not None and planned_forecast:
                            record.update(planned_budget_stop=True, planned_budget_usage=usage)
                        elif final_repair_from is not None or str(exc).startswith("global") or ctl.session_state != "running":
                            summary["stop"] = str(exc)
                        break
                    ledger.begin_decision()
                    format_budget.begin_decision()
                    first_seq = store.last_seq()
                    before_requests = audited.requests
                    before_intentions = set(ctl.state["intentions"])
                    report = cycle.run(due(ctl))
                    format_budget.active = False
                    events = [e.to_dict() for e in ctl.events_after(first_seq)]
                    appraisal_errors = artifacts.clean(assessment_errors(events))
                    traces = audited.traces[before_requests:]
                    technical_faults, resolved_format_events, classification_events, failed_technical_stages = decision_technical_faults(json_value(report), events, traces)
                    technical_faults, resolved_format_events = artifacts.clean(technical_faults), artifacts.clean(resolved_format_events)
                    valid_choice = report.outcome not in ("no_decision", "failed", "skipped") and any(valid_choice_trace(trace) for trace in traces)
                    if final_repair_from is not None and valid_choice:
                        record["valid_typed_choice_decisions"] += 1
                    entry = dict(index=index + 1, physical_decision_index=ledger.decisions, report=json_value(report),
                                 events=events, assessment_errors=appraisal_errors, technical_faults=technical_faults,
                                 resolved_format_events=resolved_format_events,
                                 state=ctl.state, session_usage=session.usage(),
                                 provider_error=audited.failure, typed_requests=audited.requests - before_requests,
                                 shared_format_budget=format_budget.snapshot(), valid_typed_choice=valid_choice)
                    artifacts.write(f"cells/{name}/decisions/{index + 1:02d}.json", entry)
                    record["decisions"].append(dict(index=index + 1, outcome=report.outcome, reason=report.reason,
                                                    new_intentions=sorted(set(ctl.state["intentions"]) - before_intentions),
                                                    assessment_errors=appraisal_errors,
                                                    actions=json_value(report.actions)))
                    summary["typed_requests"] += audited.requests - before_requests
                    was_running_before_limits = ctl.session_state == "running"
                    before_limits_seq = store.last_seq()
                    session.check_limits()
                    save()
                    tool_failure = final_repair_from is not None and any(isinstance(trace.get("response"), dict) and
                                                                         (trace["response"].get("tool_use_detected") is True or tool_use_signs(trace["response"])) for trace in traces)
                    limit_events = [e.to_dict() for e in ctl.events_after(before_limits_seq)]
                    limit_faults = [dict(source="event", event=e) for e in limit_events if e["type"] == "system.error"]
                    if limit_faults:
                        technical_faults.extend(artifacts.clean(limit_faults))
                        classification_events.extend(limit_events)
                        entry.update(technical_faults=technical_faults, limit_events=limit_events)
                        artifacts.write(f"cells/{name}/decisions/{index + 1:02d}.json", entry)
                    if final_repair_from is not None and planned_cell_call_limit(session, entry, tool_failure, was_running_before_limits, limit_events):
                        record.update(stop="cell_calls_exhausted", planned_budget_stop=True, planned_budget_usage=session.usage())
                        entry.update(planned_budget_stop="cell_calls_exhausted", session_usage_after_limits=session.usage(), limit_events=limit_events)
                        artifacts.write(f"cells/{name}/decisions/{index + 1:02d}.json", entry)
                        break
                    if technical_faults or appraisal_errors or audited.failure or report.outcome in ("no_decision", "failed", "skipped") or ctl.session_state != "running" or tool_failure:
                        if technical_faults:
                            record["technical_faults"] = technical_faults
                        if failed_technical_stages or (technical_faults and not appraisal_errors and not audited.failure and not tool_failure):
                            record.update(stop="technical_fault", error=dict(error_class="TechnicalFault", faults=technical_faults))
                        elif appraisal_errors:
                            record.update(stop="assessment_error", error=dict(error_class="AssessmentError", assessments=appraisal_errors))
                        else:
                            record.update(stop="tool_use" if tool_failure else "provider_or_cycle_failure" if audited.failure or report.outcome != "done" else "session_paused", error=audited.failure)
                        if final_repair_from is not None and not failed_technical_stages and local_format_rejection(audited.failure, appraisal_errors, traces, classification_events, ctl.session_state == "running"):
                            record["local_rejection"] = True
                            summary["local_rejections"].append(dict(cell=name, decision=index + 1, stop=record["stop"], error=record["error"],
                                                                    format_codes=traces[-1].get("format_codes", []) if traces else [],
                                                                    request_traces=[f"cells/{name}/requests/{number:03d}.json" for number in range(before_requests + 1, audited.requests + 1)]))
                        else:
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
        if final_repair_from is not None and not summary["stop"]:
            if len(summary["cells"]) != len(plan()) or any(cell.get("valid_typed_choice_decisions", 0) < 1 for cell in summary["cells"]):
                summary.update(status="partial", stop="no_valid_decision")
            else:
                summary["status"] = "completed_with_rejections" if summary["local_rejections"] else "completed"
        else:
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
    corrections = parser.add_mutually_exclusive_group()
    corrections.add_argument("--repair-from", help="Legacy single correction of an immutable partial live evidence directory")
    corrections.add_argument("--final-repair-from", help="D064 final bounded correction of immutable live version2 format failure")
    args = parser.parse_args(argv)
    if args.mode == "live" and not args.auth_file:
        parser.error("--mode live requires --auth-file")
    if args.mode == "stub" and args.auth_file:
        parser.error("--auth-file is only valid in live mode")
    if (args.repair_from or args.final_repair_from) and args.mode != "live":
        parser.error("Correction flags require --mode live")
    try:
        summary = run_series(args.output, mode=args.mode, auth_file=args.auth_file, repair_from=args.repair_from, final_repair_from=args.final_repair_from)
    except (ValueError, OSError) as exc:
        # Avoid echoing credential paths or arbitrary exception text to the terminal.
        parser.exit(2, f"Refused: {type(exc).__name__}; output must be new and auth external.\n")
    print(json.dumps({key: summary[key] for key in ("status", "actual_model_calls", "decisions_started", "new_decisions_started",
                                                  "selected_logical_decisions", "inherited_failed_decisions", "stop")}, ensure_ascii=False))
    return 0 if summary["status"] in ("completed", "completed_with_rejections") else 1


if __name__ == "__main__":
    raise SystemExit(main())
