#!/usr/bin/env python3
"""Каталог сценариев проверок: проверка формата, список, покрытие требований.

Сценарии лежат в scenarios/<группа>/<id>.toml; формат описан в docs/spec/scenario-format.md.
Только стандартная библиотека (tomllib из Python 3.11+). Модель в проверках не участвует:
утверждения сценария говорят только о наблюдаемом — журнале событий и состоянии.

Команды:
  validate              проверить все сценарии, полноту каталога и связь с requirements.toml
  list [--group G]      перечень сценариев (id, группа, заголовок, требования)
  coverage              какие требования закрыты сценариями, какие — только текстом проверки

Код возврата не нулевой, если каталог не проходит проверку.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(os.environ.get("IAM_ROOT") or Path(__file__).resolve().parent.parent).resolve()
SCEN_DIR = "scenarios"
PLAN_REL = "docs/PLAN.md"
EVENTS_REL = "docs/spec/events.md"
REQS_REL = "requirements.toml"

FORMAT_VERSION = 1
# Группа → сколько сценариев обязано быть (план §5.1: 19; §3.2: 8; §3.3: 6).
GROUPS = {"required": 19, "experiments": 8, "motivation": 6}
MAX_KEY_DECISIONS = 4            # R-127: не более четырёх ключевых решений на прогон
SWITCHES = ("memory", "motivation")
STATE_GROUPS = ("traits", "affect", "beliefs", "relations", "intentions", "preferences",
                "selfmodel", "world_time", "memory", "capabilities")
ORIGINS = ("observation", "model_knowledge", "stated_by_other", "own_hypothesis",
           "imagination", "simulation_event", "model_explanation")
BACKGROUND_KINDS = ("memory", "belief", "intention", "relation", "world", "capability", "config")
INTERVENTIONS = ("session_control", "register_capability", "grant_capability", "revoke_capability",
                 "restore_from_archive", "restore_backup", "edit_config", "technical_fix")
SESSION_REASONS = ("operator", "session_limit", "auth_lost", "repeated_failure", "crash")
PROPOSAL_KINDS = ("action", "message", "state_change", "memory_write", "intention", "creator_request", "no_action", "attend")
PURPOSES = ("appraise", "choose_action", "compose_message", "reflect", "summarize", "generalize")
DELIVERIES = ("once", "twice", "stale", "after_restore")
SENDERS = ("operator", "stranger")
FAULTS = ("auth_error", "limit", "lost_ack", "disk_full", "crash_during_action")
WORLD_OUTCOMES = ("confirmed", "failed", "no_answer")
STATE_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "in", "contains", "present", "absent", "unchanged", "changed")
TEXT_SCOPES = ("model_requests", "journal", "memory_retrieval", "state", "outbound_messages")
METRIC_SOURCES = ("journal", "state", "trace")
ASSERT_CLASSES = ("technical", "causal")
TEST_MARK = re.compile(r"[Тт]ест|[Сс]ценари|scenarios/|[Пп]рогон")
SUBJECT_RE = re.compile(r"journal:[a-z_]+\.[a-z_]+|state:\w+(\.\w+)*")
SCEN_PATH_RE = re.compile(r"scenarios/(\w+)/([\w-]+)\.toml")

STEP_KINDS = {
    # kind: (обязательные поля, необязательные поля)
    "session": ({"action"}, {"reason"}),
    "input": ({"sender", "text"}, {"channel", "deliveries"}),
    "wait": (set(), {"life_ms", "calendar_ms"}),
    "decide": ({"situation"}, set()),
    "model": ({"proposal", "delivery"}, {"content", "purpose"}),
    "world": ({"outcome", "action"}, set()),
    "operator": ({"intervention", "reason"}, {"detail"}),
    "fault": ({"fault"}, {"detail"}),
    "backup": ({"action"}, set()),
    "provider": ({"action", "to"}, set()),
}
STEP_COMMON = {"id", "kind", "run", "note"}
ASSERT_KINDS = {
    "event": ({"type"}, {"where", "count", "min", "max", "per"}),
    "absent": ({"type"}, {"where"}),
    "order": ({"first", "second"}, set()),
    "state": ({"path", "op"}, {"value", "since"}),
    "text": ({"marker", "scope", "presence"}, set()),
    "compare": ({"runs", "subject", "relation"}, set()),
    "chain": ({"effect", "caused_by"}, set()),
}
ASSERT_COMMON = {"id", "kind", "run", "class", "explain", "after", "before"}
TOP_FIELDS = {"format", "id", "group", "title", "summary", "requirements", "failure", "persona", "seed",
              "plan_row", "plan_title", "plan_expected", "background", "run", "step", "assert", "metric"}


def read_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def event_catalog(root: Path) -> tuple[set[str], list[str]]:
    """Типы событий из каталога docs/spec/events.md (между маркерами)."""
    p = root / EVENTS_REL
    if not p.is_file():
        return set(), [f"нет {EVENTS_REL}: не из чего проверять типы событий"]
    text = p.read_text(encoding="utf-8")
    m = re.search(r"<!-- каталог:начало -->(.*?)<!-- каталог:конец -->", text, re.S)
    if not m:
        return set(), [f"{EVENTS_REL}: нет маркеров каталога типов событий"]
    types = set(re.findall(r"^\|\s*`([a-z_]+\.[a-z_]+)`", m.group(1), re.M))
    return types, ([] if types else [f"{EVENTS_REL}: каталог типов событий пуст"])


def plan_table(root: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Таблица «Обязательные сценарии» плана §5.1: список (проверка, ожидаемый результат)."""
    p = root / PLAN_REL
    if not p.is_file():
        return [], [f"нет {PLAN_REL}"]
    lines = p.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("### Обязательные сценарии"))
    except StopIteration:
        return [], ["PLAN.md: нет раздела «Обязательные сценарии»"]
    rows: list[tuple[str, str]] = []
    for l in lines[start + 1:]:
        if l.startswith("#"):
            break
        if not l.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        if len(cells) != 2 or set(cells[0]) <= set("-: ") or cells[0] == "Проверка":
            continue
        rows.append((cells[0], cells[1]))
    return rows, ([] if rows else ["PLAN.md: таблица «Обязательные сценарии» пуста"])


def _is_id(v) -> bool:
    return isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_]+", v) is not None


def _nonempty(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _unknown(d: dict, allowed: set[str], where: str, errs: list[str]) -> None:
    extra = sorted(set(d) - allowed)
    if extra:
        errs.append(f"{where}: неизвестные поля {extra}")


def _check_steps(sc: dict, run_ids: set[str], group: str, errs: list[str]) -> dict[str, str | None]:
    """Проверяет шаги; возвращает {id шага: run или None}."""
    steps = sc.get("step", [])
    if not isinstance(steps, list) or not steps:
        errs.append("нет ни одного шага [[step]]")
        return {}
    ids: dict[str, str | None] = {}
    decides: dict[str, int] = defaultdict(int)
    for i, s in enumerate(steps, 1):
        sid = s.get("id", f"#{i}")
        where = f"шаг {sid}"
        if not _is_id(s.get("id")):
            errs.append(f"{where}: нужен id из латинских букв, цифр и «_»")
        elif s["id"] in ids:
            errs.append(f"{where}: id шага повторяется")
        else:
            ids[s["id"]] = s.get("run")
        kind = s.get("kind")
        if kind not in STEP_KINDS:
            errs.append(f"{where}: kind «{kind}» не из {sorted(STEP_KINDS)}")
            continue
        need, opt = STEP_KINDS[kind]
        _unknown(s, STEP_COMMON | need | opt, where, errs)
        for f in sorted(need):
            if f not in s:
                errs.append(f"{where}: для kind={kind} нужно поле {f}")
        run = s.get("run")
        if run is not None and run not in run_ids:
            errs.append(f"{where}: run «{run}» не объявлен")
        if kind == "session":
            if s.get("action") not in ("start", "pause", "resume", "stop", "crash"):
                errs.append(f"{where}: session.action должно быть start/pause/resume/stop/crash")
            if "reason" in s and s["reason"] not in SESSION_REASONS:
                errs.append(f"{where}: reason «{s['reason']}» не из {SESSION_REASONS}")
        elif kind == "input":
            if s.get("sender") not in SENDERS:
                errs.append(f"{where}: sender должно быть из {SENDERS}")
            if not _nonempty(s.get("text")):
                errs.append(f"{where}: text пустой")
            if "deliveries" in s and not _pos_int(s["deliveries"]):
                errs.append(f"{where}: deliveries должно быть положительным целым")
        elif kind == "wait":
            given = [f for f in ("life_ms", "calendar_ms") if f in s]
            if len(given) != 1:
                errs.append(f"{where}: wait требует ровно одно из life_ms / calendar_ms")
            elif not _pos_int(s[given[0]]):
                errs.append(f"{where}: {given[0]} должно быть положительным целым")
        elif kind == "decide":
            if not _nonempty(s.get("situation")):
                errs.append(f"{where}: situation пустая")
            decides[run or "*"] += 1
        elif kind == "model":
            if s.get("proposal") not in PROPOSAL_KINDS:
                errs.append(f"{where}: proposal должно быть из {PROPOSAL_KINDS}")
            if s.get("delivery") not in DELIVERIES:
                errs.append(f"{where}: delivery должно быть из {DELIVERIES}")
            if "purpose" in s and s["purpose"] not in PURPOSES:
                errs.append(f"{where}: purpose должно быть из {PURPOSES}")
        elif kind == "world":
            if s.get("outcome") not in WORLD_OUTCOMES:
                errs.append(f"{where}: outcome должно быть из {WORLD_OUTCOMES}")
            if not _nonempty(s.get("action")):
                errs.append(f"{where}: action пустое")
        elif kind == "operator":
            if s.get("intervention") not in INTERVENTIONS:
                errs.append(f"{where}: intervention должно быть из {INTERVENTIONS}")
            if not _nonempty(s.get("reason")):
                errs.append(f"{where}: у вмешательства обязательна непустая причина reason")
        elif kind == "fault":
            if s.get("fault") not in FAULTS:
                errs.append(f"{where}: fault должно быть из {FAULTS}")
        elif kind == "backup":
            if s.get("action") not in ("create", "restore"):
                errs.append(f"{where}: backup.action должно быть create/restore")
        elif kind == "provider":
            if s.get("action") != "swap" or not _nonempty(s.get("to")):
                errs.append(f"{where}: provider требует action=\"swap\" и непустое to")
    if group in ("experiments", "motivation"):
        # шаги без run относятся ко всем прогонам
        shared = decides.get("*", 0)
        for r in (sorted(run_ids) or ["main"]):
            n = shared + decides.get(r, 0)
            if n > MAX_KEY_DECISIONS:
                errs.append(f"прогон {r}: {n} ключевых решений (decide), допустимо не более {MAX_KEY_DECISIONS} (R-127)")
            if n == 0:
                errs.append(f"прогон {r}: нет ни одного ключевого решения (decide)")
    return ids


def _check_asserts(sc: dict, run_ids: set[str], step_runs: dict[str, str | None], types: set[str], group: str,
                   errs: list[str]) -> None:
    step_ids = set(step_runs)
    asserts = sc.get("assert", [])
    if not isinstance(asserts, list) or not asserts:
        errs.append("нет ни одного утверждения [[assert]]")
        return
    ids: set[str] = set()
    has_compare = False
    for i, a in enumerate(asserts, 1):
        aid = a.get("id", f"#{i}")
        where = f"утверждение {aid}"
        if not _is_id(a.get("id")):
            errs.append(f"{where}: нужен id из латинских букв, цифр и «_»")
        elif a["id"] in ids:
            errs.append(f"{where}: id повторяется")
        else:
            ids.add(a["id"])
        if not _nonempty(a.get("explain")):
            errs.append(f"{where}: нет explain (что именно проверяется и что означает провал)")
        cls = a.get("class")
        if cls is not None and cls not in ASSERT_CLASSES:
            errs.append(f"{where}: class должно быть из {ASSERT_CLASSES}")
        if group in ("experiments", "motivation") and cls is None:
            errs.append(f"{where}: в группе {group} у утверждения обязателен class (technical/causal)")
        run = a.get("run")
        if run is not None and run not in run_ids:
            errs.append(f"{where}: run «{run}» не объявлен")
        kind = a.get("kind")
        if kind not in ASSERT_KINDS:
            errs.append(f"{where}: kind «{kind}» не из {sorted(ASSERT_KINDS)}")
            continue
        need, opt = ASSERT_KINDS[kind]
        _unknown(a, ASSERT_COMMON | need | opt, where, errs)
        for f in sorted(need):
            if f not in a:
                errs.append(f"{where}: для kind={kind} нужно поле {f}")
        for f in ("after", "before", "since"):
            if f not in a:
                continue
            if a[f] not in step_ids:
                errs.append(f"{where}: {f} ссылается на несуществующий шаг «{a[f]}»")
            elif step_runs[a[f]] is not None and step_runs[a[f]] != run:
                errs.append(f"{where}: {f} ссылается на шаг «{a[f]}» прогона «{step_runs[a[f]]}», а утверждение относится к "
                            + (f"прогону «{run}»" if run else "всем прогонам"))
        for f in ("type", "first", "second", "effect", "caused_by"):
            if f in a and a[f] not in types:
                errs.append(f"{where}: тип события «{a[f]}» не из каталога docs/spec/events.md")
        if kind == "event":
            counts = [f for f in ("count", "min", "max") if f in a]
            for f in counts:
                if not isinstance(a[f], int) or isinstance(a[f], bool) or a[f] < 0:
                    errs.append(f"{where}: {f} должно быть неотрицательным целым")
            if "count" in a and len(counts) > 1:
                errs.append(f"{where}: count нельзя сочетать с min/max")
            if "per" in a and a["per"] != "cycle":
                errs.append(f"{where}: per может быть только «cycle»")
        if "where" in a and (not isinstance(a["where"], dict) or not a["where"]):
            errs.append(f"{where}: where должно быть непустой таблицей «поле = значение»")
        if kind == "state":
            path = a.get("path")
            if not _nonempty(path) or path.split(".")[0] not in STATE_GROUPS:
                errs.append(f"{where}: path должен начинаться с одной из групп {STATE_GROUPS}")
            op = a.get("op")
            if op not in STATE_OPS:
                errs.append(f"{where}: op должно быть из {STATE_OPS}")
            elif op in ("absent", "unchanged", "changed"):
                if "value" in a:
                    errs.append(f"{where}: для op={op} поле value не используется")
                if op in ("unchanged", "changed") and "since" not in a:
                    errs.append(f"{where}: для op={op} нужен since (шаг, с которого сравниваем)")
            elif op == "present":
                if "value" in a:
                    errs.append(f"{where}: для op=present поле value не используется")
            elif "value" not in a:
                errs.append(f"{where}: для op={op} нужно поле value")
        elif kind == "text":
            if not _nonempty(a.get("marker")):
                errs.append(f"{where}: marker пустой")
            if a.get("scope") not in TEXT_SCOPES:
                errs.append(f"{where}: scope должно быть из {TEXT_SCOPES}")
            if a.get("presence") not in ("present", "absent"):
                errs.append(f"{where}: presence должно быть present/absent")
        elif kind == "compare":
            has_compare = True
            rs = a.get("runs")
            if not (isinstance(rs, list) and len(rs) == 2 and rs[0] != rs[1] and all(r in run_ids for r in rs)):
                errs.append(f"{where}: runs — два разных объявленных прогона")
            if not (isinstance(a.get("subject"), str) and SUBJECT_RE.fullmatch(a["subject"])):
                errs.append(f"{where}: subject — «journal:<тип события>» или «state:<путь>»")
            if a.get("relation") not in ("differs", "same"):
                errs.append(f"{where}: relation должно быть differs/same")
    if len(run_ids) > 1 and not has_compare:
        errs.append("несколько прогонов (run), но нет утверждения kind=compare")
    if group in ("experiments", "motivation"):
        classes = {a.get("class") for a in asserts}
        for c in ASSERT_CLASSES:
            if c not in classes:
                errs.append(f"в группе {group} нужно хотя бы одно утверждение class={c}")


def validate_scenario(sc: dict, stem: str, dir_group: str, req_ids: set[str], types: set[str]) -> list[str]:
    """Ошибки одного сценария. stem — имя файла без .toml, dir_group — имя каталога-группы."""
    errs: list[str] = []
    _unknown(sc, TOP_FIELDS, "сценарий", errs)
    if sc.get("format") != FORMAT_VERSION:
        errs.append(f"format должно быть {FORMAT_VERSION}")
    if sc.get("id") != stem:
        errs.append(f"id «{sc.get('id')}» не совпадает с именем файла «{stem}»")
    group = sc.get("group")
    if group != dir_group:
        errs.append(f"group «{group}» не совпадает с каталогом «{dir_group}»")
    for f in ("title", "summary", "failure"):
        if not _nonempty(sc.get(f)):
            errs.append(f"поле {f} отсутствует или пустое" + (" (признак провала обязателен)" if f == "failure" else ""))
    reqs = sc.get("requirements")
    if not (isinstance(reqs, list) and reqs and all(isinstance(r, str) for r in reqs)):
        errs.append("requirements — непустой список ID требований")
    else:
        if len(set(reqs)) != len(reqs):
            errs.append("requirements: ID повторяются")
        for r in reqs:
            if r not in req_ids:
                errs.append(f"requirements: требование {r} не существует в requirements.toml")
    if group == "required":
        pr = sc.get("plan_row")
        if not (isinstance(pr, int) and not isinstance(pr, bool) and pr >= 1):
            errs.append("для required нужен plan_row (номер строки таблицы §5.1, с 1)")
        for f in ("plan_title", "plan_expected"):
            if not _nonempty(sc.get(f)):
                errs.append(f"для required нужно поле {f} (дословно из таблицы §5.1)")
    elif any(f in sc for f in ("plan_row", "plan_title", "plan_expected")):
        errs.append("plan_row/plan_title/plan_expected — только для группы required")
    # прогоны
    runs = sc.get("run", [])
    run_ids: set[str] = set()
    if not isinstance(runs, list):
        errs.append("run должно быть списком таблиц [[run]]")
        runs = []
    for i, r in enumerate(runs, 1):
        w = f"run #{i}"
        _unknown(r, {"id", "switches", "note"}, w, errs)
        if not _is_id(r.get("id")):
            errs.append(f"{w}: нужен id")
        elif r["id"] in run_ids:
            errs.append(f"{w}: id повторяется")
        else:
            run_ids.add(r["id"])
        sw = r.get("switches", {})
        if not isinstance(sw, dict) or any(k not in SWITCHES or v not in ("on", "off") for k, v in sw.items()):
            errs.append(f"{w}: switches — таблица с ключами {SWITCHES} и значениями on/off")
    if len(runs) == 1:
        errs.append("одиночный [[run]] не нужен: без [[run]] сценарий идёт одним прогоном; нужно ≥ 2 прогонов")
    # предыстория
    bg = sc.get("background", [])
    if not isinstance(bg, list):
        errs.append("background должно быть списком таблиц [[background]]")
        bg = []
    for i, b in enumerate(bg, 1):
        w = f"background #{i}"
        _unknown(b, {"id", "kind", "text", "origin", "run", "note"}, w, errs)
        if "run" in b and b["run"] not in run_ids:
            errs.append(f"{w}: run «{b['run']}» не объявлен")
        if b.get("kind") not in BACKGROUND_KINDS:
            errs.append(f"{w}: kind должно быть из {BACKGROUND_KINDS}")
        if not _nonempty(b.get("text")):
            errs.append(f"{w}: text пустой")
        if "origin" in b and b["origin"] not in ORIGINS:
            errs.append(f"{w}: origin «{b['origin']}» не из {ORIGINS}")
    step_runs = _check_steps(sc, run_ids, group, errs)
    _check_asserts(sc, run_ids, step_runs, types, group, errs)
    # метрики
    metrics = sc.get("metric", [])
    if not isinstance(metrics, list):
        errs.append("metric должно быть списком таблиц [[metric]]")
        metrics = []
    if group == "motivation" and not metrics:
        errs.append("для motivation нужна хотя бы одна метрика [[metric]] (R-132)")
    mids: set[str] = set()
    for i, m in enumerate(metrics, 1):
        w = f"metric #{i}"
        _unknown(m, {"id", "definition", "source"}, w, errs)
        if not _is_id(m.get("id")) or m["id"] in mids:
            errs.append(f"{w}: нужен уникальный id")
        else:
            mids.add(m["id"])
        if not _nonempty(m.get("definition")):
            errs.append(f"{w}: нет definition (как считается по журналу/состоянию)")
        if m.get("source") not in METRIC_SOURCES:
            errs.append(f"{w}: source должно быть из {METRIC_SOURCES}")
    return errs


def load_catalog(root: Path) -> tuple[dict[str, dict], list[str]]:
    """Читает все scenarios/<группа>/*.toml. Возвращает {путь: данные} и ошибки чтения."""
    out: dict[str, dict] = {}
    errs: list[str] = []
    base = root / SCEN_DIR
    if not base.is_dir():
        return out, [f"нет каталога {SCEN_DIR}/"]
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        if d.name not in GROUPS:
            errs.append(f"{SCEN_DIR}/{d.name}: неизвестная группа (допустимо {sorted(GROUPS)})")
            continue
        for f in sorted(d.glob("*.toml")):
            rel = f"{SCEN_DIR}/{d.name}/{f.name}"
            try:
                out[rel] = read_toml(f)
            except (tomllib.TOMLDecodeError, OSError) as e:
                errs.append(f"{rel}: не читается ({e})")
    return out, errs


def validate_catalog(root: Path, reqs: list[dict]) -> list[str]:
    errs: list[str] = []
    req_ids = {r["id"] for r in reqs}
    types, e = event_catalog(root)
    errs += e
    catalog, e = load_catalog(root)
    errs += e
    seen_ids: dict[str, str] = {}
    for rel, sc in catalog.items():
        group, stem = rel.split("/")[1], Path(rel).stem
        for m in validate_scenario(sc, stem, group, req_ids, types):
            errs.append(f"{rel}: {m}")
        sid = sc.get("id")
        if sid in seen_ids:
            errs.append(f"{rel}: id «{sid}» уже есть в {seen_ids[sid]}")
        seen_ids[sid] = rel
    # полнота групп
    for g, n in GROUPS.items():
        have = [r for r in catalog if r.split("/")[1] == g]
        if len(have) != n:
            errs.append(f"группа {g}: сценариев {len(have)}, нужно {n}")
    # таблица плана §5.1: каждая строка — ровно в одном сценарии, дословно
    rows, e = plan_table(root)
    errs += e
    if rows:
        if len(rows) != GROUPS["required"]:
            errs.append(f"PLAN.md §5.1: в таблице {len(rows)} строк, ожидалось {GROUPS['required']}")
        by_row: dict[int, list[str]] = defaultdict(list)
        for rel, sc in catalog.items():
            if sc.get("group") == "required" and isinstance(sc.get("plan_row"), int):
                by_row[sc["plan_row"]].append(rel)
        for n, (title, expected) in enumerate(rows, 1):
            files = by_row.get(n, [])
            if len(files) != 1:
                errs.append(f"строка {n} таблицы §5.1 («{title}») присутствует в {len(files)} сценариях, нужно ровно в одном")
                continue
            sc = catalog[files[0]]
            if sc.get("plan_title") != title:
                errs.append(f"{files[0]}: plan_title не совпадает с таблицей плана («{title}»)")
            if sc.get("plan_expected") != expected:
                errs.append(f"{files[0]}: plan_expected не совпадает с таблицей плана («{expected}»)")
        for n in by_row:
            if not 1 <= n <= len(rows):
                errs.append(f"plan_row {n} вне диапазона 1–{len(rows)} ({', '.join(by_row[n])})")
    errs += link_problems(catalog, reqs)
    return errs


def link_problems(catalog: dict[str, dict], reqs: list[dict]) -> list[str]:
    """Двусторонняя связь: требование из сценария ссылается на него в check, и наоборот;
    каждое требование kind=functional имеет сценарий либо тест/прогон в тексте проверки."""
    errs: list[str] = []
    by_id = {r["id"]: r for r in reqs}
    listed: dict[str, set[str]] = defaultdict(set)      # R-… → пути сценариев, которые его перечисляют
    for rel, sc in catalog.items():
        for rid in sc.get("requirements", []) if isinstance(sc.get("requirements"), list) else []:
            listed[rid].add(rel)
            r = by_id.get(rid)
            if r is not None and rel not in r["check"]:
                errs.append(f"{rid}: сценарий {rel} перечисляет требование, но его check не ссылается на сценарий")
    for r in reqs:
        for g, name in SCEN_PATH_RE.findall(r["check"]):
            rel = f"{SCEN_DIR}/{g}/{name}.toml"
            if rel not in catalog:
                errs.append(f"{r['id']}: check ссылается на несуществующий сценарий {rel}")
            elif r["id"] not in listed or rel not in listed[r["id"]]:
                errs.append(f"{r['id']}: check ссылается на {rel}, но сценарий не перечисляет это требование")
    for r in reqs:
        if r["kind"] == "functional" and r["id"] not in listed and not TEST_MARK.search(r["check"]):
            errs.append(f"{r['id']}: функциональное требование без сценария и без теста/прогона в check")
    return errs


# ---------------------------------------------------------------- команды


def get_inputs() -> list[dict]:
    p = ROOT / REQS_REL
    if not p.is_file():
        sys.exit(f"ОШИБКА: нет {REQS_REL}")
    return read_toml(p)["req"]


def cmd_validate(args) -> int:
    reqs = get_inputs()
    errs = validate_catalog(ROOT, reqs)
    if errs:
        print(f"КАТАЛОГ СЦЕНАРИЕВ НЕ ПРОХОДИТ ПРОВЕРКУ ({len(errs)}):")
        for e in errs:
            print("  -", e)
        return 1
    catalog, _ = load_catalog(ROOT)
    parts = ", ".join(f"{g} {sum(1 for r in catalog if r.split('/')[1] == g)}" for g in GROUPS)
    print(f"OK: сценариев {len(catalog)} ({parts}); связь с requirements.toml двусторонняя")
    return 0


def cmd_list(args) -> int:
    catalog, errs = load_catalog(ROOT)
    for e in errs:
        print("ВНИМАНИЕ:", e)
    for rel, sc in catalog.items():
        g = rel.split("/")[1]
        if args.group and g != args.group:
            continue
        reqs = ",".join(sc.get("requirements", []))
        print(f"{g}/{sc.get('id')}: {sc.get('title')} [{reqs}]")
    return 0


def cmd_coverage(args) -> int:
    reqs = get_inputs()
    catalog, errs = load_catalog(ROOT)
    for e in errs:
        print("ВНИМАНИЕ:", e)
    listed: dict[str, list[str]] = defaultdict(list)
    for rel, sc in catalog.items():
        for rid in sc.get("requirements", []):
            listed[rid].append(Path(rel).stem)
    func = [r for r in reqs if r["kind"] == "functional"]
    via_sc = [r for r in func if r["id"] in listed]
    via_text = [r for r in func if r["id"] not in listed and TEST_MARK.search(r["check"])]
    none = [r for r in func if r["id"] not in listed and not TEST_MARK.search(r["check"])]
    print(f"функциональных требований: {len(func)}")
    print(f"  закрыто сценарием каталога: {len(via_sc)}")
    print(f"  только тестом/прогоном по тексту check: {len(via_text)}")
    print(f"  без сценария и без теста: {len(none)}")
    for r in none:
        print(f"    {r['id']}: {r['text'][:80]}")
    print(f"\nвсего требований, на которые ссылаются сценарии: {len(listed)} из {len(reqs)}")
    for rid in sorted(listed):
        print(f"  {rid}: {', '.join(listed[rid])}")
    return 1 if none else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Каталог сценариев проверок")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    li = sub.add_parser("list")
    li.add_argument("--group", choices=sorted(GROUPS))
    sub.add_parser("coverage")
    args = ap.parse_args(argv)
    return {"validate": cmd_validate, "list": cmd_list, "coverage": cmd_coverage}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
