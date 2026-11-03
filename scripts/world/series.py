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
from iam.domain import jsonschema, new_id
from iam.interfaces import ActionOutcome, ProviderError
from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.run import subprocess_runner
from iam.session import SessionRunner
from iam.state import StateController
from iam.storage import Store
from iam.stubs import ScriptedProvider, Reply, StubCapabilities, StubCreatorBridge, act, rest
from iam.world import CAPABILITIES, PersistentWorld, READ, WRITE
from iam.world.cycle import GUIDANCE, LivingCycle, WorldMemory

ROOT = Path(__file__).resolve().parents[2]
VERSION = "s22-series-2"
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
    def __init__(self, inner, artifacts, cell, *, disclose=True):
        self.inner, self.artifacts, self.cell, self.disclose = inner, artifacts, cell, disclose
        self.name = inner.name
        self.requests, self.failure = 0, None

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


def preserve_repair(repair, output, frozen):
    old_sources = repair["manifest"]["sources"]
    permitted = {"runtime/intentions/completion.py", "scripts/world/series.py", "scripts/world/README.md"}
    require(set(old_sources) == set(frozen), "source inventory changed")
    changed = {name for name in frozen if frozen[name] != old_sources[name]}
    require(changed <= permitted, "free-choice/core sources changed beyond the correction")
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
        cell["reused_from"] = dict(directory=str(repair["root"]), manifest_sha256=repair["inventory"]["manifest.json"])
    return dict(directory=str(repair["root"]), manifest_sha256=repair["inventory"]["manifest.json"],
                summary_sha256=repair["inventory"]["summary.json"], calls_sha256=repair["inventory"]["calls.json"],
                artifact_sha256=repair["inventory"], changed_sources=sorted(changed),
                unchanged_source_sha256={k: v for k, v in frozen.items() if k not in permitted},
                free_choice_disclosure_unchanged=True, world_guidance_source_unchanged=True,
                disclosure_sha256=hashlib.sha256(DISCLOSURE.encode()).hexdigest(),
                guidance_sha256=hashlib.sha256(GUIDANCE.encode()).hexdigest(),
                guidance_source_sha256=frozen["runtime/world/cycle.py"],
                prefix_had_no_appraisals=True)


def run_series(output, *, mode="stub", auth_file=None, provider_factory=None, scanner=None, repair_from=None):
    if mode not in ("stub", "live"):
        raise ValueError("Unknown mode")
    if mode == "live" and not auth_file:
        raise ValueError("Live mode requires an external --auth-file")
    if repair_from is not None and mode != "live":
        raise ValueError("Correction requires live mode; tests must mock the provider/transport")
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output must be a NEW directory; resume/overwrite is forbidden")
    private_paths = ()
    if auth_file is not None:
        auth = Path(auth_file).expanduser().absolute()
        if auth == output or output in auth.parents:
            raise ValueError("Credentials must be external to the output directory")
        if repair_from is not None:
            original_root = Path(repair_from).expanduser().absolute().resolve()
            require(not auth.is_relative_to(original_root) and not auth.resolve().is_relative_to(original_root),
                    "credentials must be external to the immutable original")
        private_paths = tuple({path for path in (str(auth_file), str(auth), str(auth.resolve())) if len(path) >= 4})
    artifacts = Artifacts(output, scanner, private_paths=private_paths)
    repair = load_repair(repair_from, output, artifacts) if repair_from is not None else None
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
        summary.update(repair_count=1, repair_of=str(repair["root"]),
                       inherited_failed_history=dict(cell=repair["failed_cell"], decisions=copy.deepcopy(repair["summary"]["cells"][-1]["decisions"]),
                                                     evidence="original/cells/" + repair["failed_cell"]),
                       interpretation="One authorized protocol correction. Physical counters include every original attempt. selected_logical_decisions selects the completed prefix and new branch; it is not an accepted-proposal or successful-action count. Both original failed-cell outcomes remain in original/.")

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
                        budgets=dict(global_decisions=MAX_DECISIONS, actual_model_calls=MAX_CALLS,
                                     cell_model_calls=CELL_CALLS, cell_ms=CELL_MS, forecast_calls=FORECAST,
                                     provider_format_retries=0, cycle_format_retries=1, cycle_stale_retries=0),
                        preparation_disclosure=DISCLOSURE, scripted_past=True,
                        observations="Prepared histories use real local world actions driven by scripts. No initial interest/need/skill scores are fabricated. Scenario TOML expectations are historical hypotheses, never model instructions or automatic success verdicts.",
                        credentials="External auth_file passed only to the official provider; contents and path are not evidence artifacts.",
                        transport="Every actual Codex model invocation, including appraisals and repairs, is charged before subprocess_runner. Native timeout cleanup remains enabled; docker kill is technical cleanup, not a model request.")
        if repair:
            manifest.update(repair_count=1, repair_of=repair_lineage, original_plan=plan(),
                            inherited_decisions_started=inherited_decisions, inherited_model_calls=inherited_calls,
                            inherited_typed_requests=inherited_typed, inherited_failed_decisions=inherited_failed,
                            new_decisions_max=sum(c["new_max_decisions"] for c in execution_plan),
                            selected_logical_decisions_max=MAX_DECISIONS - inherited_failed,
                            accounting="Physical decisions = inherited + new <=60; actual model invocations = inherited + new <=240. Selected logical decisions exclude the superseded failed-cell branch; all its real outcomes are retained.")
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

                def transport(argv, stdin, timeout_s, cleanup=None):
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
                    inner = CodexProvider(transport, CodexSandboxConfig(auth_file=str(Path(auth_file).expanduser()), persist_auth_updates=True), model=MODEL, timeout_s=120, max_format_retries=0)
                audited = AuditedProvider(inner, artifacts, "cells/" + name)
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
                    entry = dict(index=index + 1, physical_decision_index=ledger.decisions, report=json_value(report),
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
    parser.add_argument("--repair-from", help="One authorized correction of an immutable partial live evidence directory")
    args = parser.parse_args(argv)
    if args.mode == "live" and not args.auth_file:
        parser.error("--mode live requires --auth-file")
    if args.mode == "stub" and args.auth_file:
        parser.error("--auth-file is only valid in live mode")
    if args.repair_from and args.mode != "live":
        parser.error("--repair-from requires --mode live")
    try:
        summary = run_series(args.output, mode=args.mode, auth_file=args.auth_file, repair_from=args.repair_from)
    except (ValueError, OSError) as exc:
        # Avoid echoing credential paths or arbitrary exception text to the terminal.
        parser.exit(2, f"Refused: {type(exc).__name__}; output must be new and auth external.\n")
    print(json.dumps({key: summary[key] for key in ("status", "actual_model_calls", "decisions_started", "new_decisions_started",
                                                  "selected_logical_decisions", "inherited_failed_decisions", "stop")}, ensure_ascii=False))
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
