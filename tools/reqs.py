#!/usr/bin/env python3
"""Реестр требований: проверка, сводка, список без свидетельств, покрытие плана.

Источник данных — requirements.toml в корне репозитория (записи [[req]] и [[skip]]).
Только стандартная библиотека (tomllib из Python 3.11+).

Команды:
  validate                проверить реестр (поля, уникальность, срезы, строки плана, покрытие PLAN.md)
  report                  сводка по статусам, срезам, видам и разделам плана
  unverified [--slice S]  требования без свидетельства (всё, что не verified/waived)
  coverage [--write FILE | --check FILE]
                          таблица «раздел плана → ID требований»; --write записывает её в файл,
                          --check сверяет файл с реестром

Код возврата не нулевой, если реестр не проходит проверку.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(os.environ.get("IAM_ROOT") or Path(__file__).resolve().parent.parent).resolve()
REQS = ROOT / "requirements.toml"
PLAN = ROOT / "docs" / "PLAN.md"
STATE_JSON = ROOT / "docs" / "state.json"

KINDS = ("functional", "constraint", "process", "quality")
STATUSES = ("planned", "implemented", "verified", "waived")
REQUIRED = ("id", "source", "text", "kind", "mechanism", "check", "slice", "status", "evidence")
ID_RE = re.compile(r"^R-\d{3,}$")
EXTRA_SOURCES = ("CLAUDE.md",)

# Разделы плана: (ключ, начало строки-заголовка). Раздел длится до следующего заголовка из списка.
SECTIONS = [
    ("§0", "# План реализации"),
    ("§1", "## 1. "),
    ("§2", "## 2. "),
    ("§2.1", "### Единое состояние"),
    ("§2.2", "### Восприятие и внимание"),
    ("§2.3", "### Память и происхождение"),
    ("§2.4", "### Эмоции, убеждения"),
    ("§2.5", "### Мотивация и собственный мир"),
    ("§2.6", "### Самомодель"),
    ("§2.7", "### Возможности, инструменты"),
    ("§2.8", "### Граница между моделью"),
    ("§3", "## 3. "),
    ("§3.1", "### Сначала — техническая"),
    ("§3.2", "### Сравнение устройства интеллекта"),
    ("§3.3", "### Сравнение мотивации"),
    ("§3.4", "### Правило выбора"),
    ("§4", "## 4. "),
    ("§5", "## 5. "),
    ("§5.1", "### Обязательные сценарии"),
    ("§5.2", "### Ограничения первых сеансов"),
    ("§5.3", "### Сохранность и обслуживание"),
    ("§5.4", "### Итоговая готовность"),
]
SECTION_KEYS = [k for k, _ in SECTIONS]


def load_toml(path: Path = REQS) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def slice_ids() -> set[str]:
    return set(json.loads(STATE_JSON.read_text(encoding="utf-8"))["slices"])


def plan_lines() -> list[str]:
    return PLAN.read_text(encoding="utf-8").splitlines()


def section_ranges(lines: list[str]) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Ключ раздела → (первая строка, последняя строка), нумерация с 1. Вторым значением — ошибки."""
    errs: list[str] = []
    starts: list[tuple[str, int]] = []
    for key, prefix in SECTIONS:
        found = [i + 1 for i, l in enumerate(lines) if l.startswith(prefix)]
        if len(found) != 1:
            errs.append(f"PLAN.md: заголовок раздела {key} («{prefix}») найден {len(found)} раз, нужен один")
            continue
        starts.append((key, found[0]))
    starts.sort(key=lambda x: x[1])
    ranges = {}
    for i, (key, st) in enumerate(starts):
        end = starts[i + 1][1] - 1 if i + 1 < len(starts) else len(lines)
        ranges[key] = (st, end)
    return ranges, errs


def parse_lines(spec, errs: list[str], where: str) -> set[int]:
    """'5', '41-48', '5,7-9' → множество номеров строк."""
    out: set[int] = set()
    if not isinstance(spec, str) or not spec.strip():
        errs.append(f"{where}: lines должно быть непустой строкой вида '5' или '41-48'")
        return out
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if not m:
            errs.append(f"{where}: неверная запись строк «{part.strip()}»")
            continue
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a < 1 or b < a:
            errs.append(f"{where}: неверный диапазон «{part.strip()}»")
            continue
        out.update(range(a, b + 1))
    return out


def evidence_paths(ev: str) -> list[str]:
    """Свидетельство: записи через ';', первая «лексема» каждой — путь от корня репозитория."""
    paths = []
    for part in ev.split(";"):
        part = part.strip()
        if part:
            paths.append(part.split()[0].split("::")[0])
    return paths


def validate_data(data: dict, slices: set[str], plan: list[str] | None) -> list[str]:
    errs: list[str] = []
    reqs = data.get("req")
    if not isinstance(reqs, list) or not reqs:
        return ["в requirements.toml нет записей [[req]]"]
    ranges: dict[str, tuple[int, int]] = {}
    if plan is not None:
        ranges, e = section_ranges(plan)
        errs += e
    seen: set[str] = set()
    covered: set[int] = set()
    for idx, r in enumerate(reqs, 1):
        rid = r.get("id", f"#{idx}")
        where = f"{rid}"
        for f in REQUIRED:
            if f not in r:
                errs.append(f"{where}: нет поля {f}")
        if any(f not in r for f in REQUIRED):
            continue
        if not isinstance(r["id"], str) or not ID_RE.match(r["id"]):
            errs.append(f"{where}: id должен иметь вид R-001")
        if r["id"] in seen:
            errs.append(f"{where}: id повторяется")
        seen.add(r["id"])
        for f in ("text", "mechanism", "check"):
            if not isinstance(r[f], str) or not r[f].strip():
                errs.append(f"{where}: поле {f} пустое")
        if r["kind"] not in KINDS:
            errs.append(f"{where}: kind «{r['kind']}» не из {KINDS}")
        if r["status"] not in STATUSES:
            errs.append(f"{where}: status «{r['status']}» не из {STATUSES}")
        sl = r["slice"]
        sl_list = [sl] if isinstance(sl, str) else sl
        if not isinstance(sl_list, list) or not sl_list:
            errs.append(f"{where}: slice — строка или список строк")
        else:
            for s in sl_list:
                if s not in slices:
                    errs.append(f"{where}: срез «{s}» не существует в docs/state.json")
        src = r["source"]
        if src not in SECTION_KEYS and src not in EXTRA_SOURCES:
            errs.append(f"{where}: source «{src}» неизвестен")
        if src in EXTRA_SOURCES:
            if "lines" in r:
                errs.append(f"{where}: для source «{src}» поле lines не используется")
        else:
            if "lines" not in r:
                errs.append(f"{where}: нет поля lines (строки PLAN.md)")
            else:
                ls = parse_lines(r["lines"], errs, where)
                covered |= ls
                if plan is not None:
                    if any(n > len(plan) for n in ls):
                        errs.append(f"{where}: строки за пределами PLAN.md ({len(plan)} строк)")
                    if src in ranges:
                        a, b = ranges[src]
                        out = sorted(n for n in ls if not a <= n <= b)
                        if out:
                            errs.append(f"{where}: строки {out} не в разделе {src} (строки {a}–{b})")
        # свидетельство
        ev = r["evidence"]
        if not isinstance(ev, str):
            errs.append(f"{where}: evidence должно быть строкой")
            ev = ""
        if r["status"] == "verified" and not ev.strip():
            errs.append(f"{where}: verified без свидетельства (evidence пусто)")
        if r["status"] == "waived":
            if not str(r.get("waiver", "")).strip():
                errs.append(f"{where}: waived требует поле waiver с обоснованием")
        if r["status"] in ("verified", "implemented") and ev.strip():
            for p in evidence_paths(ev):
                if p.startswith("/") or ".." in Path(p).parts or not (ROOT / p).exists():
                    errs.append(f"{where}: свидетельство «{p}» не найдено в репозитории")
        if "stub_only" in r and not isinstance(r["stub_only"], bool):
            errs.append(f"{where}: stub_only должно быть true/false")
    # пропуски
    skips = data.get("skip", [])
    skip_lines: set[int] = set()
    for i, s in enumerate(skips, 1):
        where = f"skip #{i}"
        if not str(s.get("why", "")).strip():
            errs.append(f"{where}: нет обоснования why")
        skip_lines |= parse_lines(s.get("lines"), errs, where)
    # покрытие плана
    if plan is not None:
        both = sorted(covered & skip_lines)
        if both:
            errs.append(f"строки одновременно в требованиях и в пропусках: {compress(both)}")
        missing = [n for n, l in enumerate(plan, 1) if l.strip() and n not in covered and n not in skip_lines]
        if missing:
            errs.append(f"строки PLAN.md без требования и без пропуска: {compress(missing)}")
        extra = sorted(n for n in skip_lines if n > len(plan) or (n >= 1 and not plan[n - 1].strip()))
        if extra:
            errs.append(f"пропуски указывают на пустые или несуществующие строки: {compress(extra)}")
    return errs


def compress(nums) -> str:
    nums = sorted(nums)
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}–{nums[j]}")
        i = j + 1
    return ", ".join(out)


def slices_of(r: dict) -> list[str]:
    return [r["slice"]] if isinstance(r["slice"], str) else list(r["slice"])


# ---------------------------------------------------------------- команды


def get_inputs():
    if not REQS.exists():
        sys.exit(f"ОШИБКА: нет {REQS.relative_to(ROOT)}")
    data = load_toml()
    plan = plan_lines() if PLAN.exists() else None
    return data, slice_ids(), plan


def cmd_validate(args) -> int:
    data, slices, plan = get_inputs()
    errs = validate_data(data, slices, plan)
    if errs:
        print(f"РЕЕСТР НЕ ПРОХОДИТ ПРОВЕРКУ ({len(errs)}):")
        for e in errs:
            print("  -", e)
        return 1
    n = len(data["req"])
    cov = "" if plan is None else f"; все непустые строки PLAN.md ({len(plan)}) покрыты требованиями или пропусками"
    print(f"OK: {n} требований{cov}")
    return 0


def status_counts(reqs: list[dict]) -> Counter:
    c = Counter(r["status"] for r in reqs)
    c["verified_stub"] = sum(1 for r in reqs if r["status"] == "verified" and r.get("stub_only"))
    return c


def summary_line(reqs: list[dict]) -> str:
    c = status_counts(reqs)
    s = (f"требований {len(reqs)}: verified {c['verified']}"
         + (f" (из них на заглушке {c['verified_stub']})" if c["verified_stub"] else "")
         + f", implemented {c['implemented']}, planned {c['planned']}, waived {c['waived']}")
    return s


def cmd_report(args) -> int:
    data, slices, plan = get_inputs()
    errs = validate_data(data, slices, plan)
    if errs:
        print(f"ВНИМАНИЕ: реестр не проходит проверку ({len(errs)} замечаний); запустите validate")
    reqs = data["req"]
    print(summary_line(reqs))
    print("\nПо видам:")
    for k, n in sorted(Counter(r["kind"] for r in reqs).items()):
        print(f"  {k}: {n}")
    print("\nПо срезам (всего / не verified):")
    per: dict[str, list[dict]] = defaultdict(list)
    for r in reqs:
        for s in slices_of(r):
            per[s].append(r)
    for s in sorted(per):
        open_n = sum(1 for r in per[s] if r["status"] not in ("verified", "waived"))
        print(f"  {s}: {len(per[s])} / {open_n}")
    print("\nПо разделам плана:")
    persec = Counter(r["source"] for r in reqs)
    for k in SECTION_KEYS + list(EXTRA_SOURCES):
        if persec.get(k):
            print(f"  {k}: {persec[k]}")
    return 1 if errs else 0


def cmd_unverified(args) -> int:
    data, slices, plan = get_inputs()
    rows = [r for r in data["req"] if r["status"] not in ("verified", "waived")]
    if args.slice:
        rows = [r for r in rows if args.slice in slices_of(r)]
    for r in rows:
        print(f"{r['id']} [{','.join(slices_of(r))}] {r['status']}: {r['text']}")
    print(f"\nбез свидетельства: {len(rows)} из {len(data['req'])}")
    return 0


def render_coverage(data: dict, plan: list[str] | None) -> str:
    reqs = data["req"]
    ranges = section_ranges(plan)[0] if plan else {}
    titles = {}
    if plan:
        for key, prefix in SECTIONS:
            for l in plan:
                if l.startswith(prefix):
                    titles[key] = l.lstrip("# ").strip()
    out = [
        "# Покрытие плана требованиями",
        "",
        "Файл создаётся командой `python3 tools/reqs.py coverage --write docs/spec/plan-coverage.md` — вручную не править.",
        "Доказательство полноты: `python3 tools/reqs.py validate` проверяет, что каждая непустая строка `docs/PLAN.md` "
        "относится хотя бы к одному требованию либо явно перечислена в пропусках ниже с обоснованием.",
        "",
        "## Раздел плана → требования",
        "",
        "| Раздел | Заголовок | Строки PLAN.md | Требования |",
        "|---|---|---|---|",
    ]
    by_src: dict[str, list[str]] = defaultdict(list)
    for r in reqs:
        by_src[r["source"]].append(r["id"])
    for key in SECTION_KEYS + list(EXTRA_SOURCES):
        ids = by_src.get(key, [])
        rng = ranges.get(key)
        rng_s = f"{rng[0]}–{rng[1]}" if rng else "—"
        ttl = titles.get(key, "правила проекта (CLAUDE.md)" if key in EXTRA_SOURCES else "")
        out.append(f"| {key} | {ttl} | {rng_s} | {_ids_compact(ids) if ids else 'нет (только заголовок/пропуски)'} |")
    out += ["", "## Явные пропуски (строки плана без требования)", ""]
    skips = data.get("skip", [])
    if not skips:
        out.append("Пропусков нет.")
    else:
        out += ["| Строки | Что это | Почему не требование |", "|---|---|---|"]
        for s in skips:
            text = ""
            if plan:
                first = sorted(parse_lines(s["lines"], [], ""))[0]
                text = plan[first - 1].strip().replace("|", "\\|")[:70]
            out.append(f"| {s['lines']} | {text} | {s['why']} |")
    out += ["", "## Требования по строкам плана", ""]
    out += ["| Строки | Требования |", "|---|---|"]
    per_line: dict[str, list[str]] = defaultdict(list)
    for r in reqs:
        if "lines" in r:
            per_line[r["lines"]].append(r["id"])

    def keyf(s):
        return int(re.match(r"\d+", s).group())
    for ls in sorted(per_line, key=keyf):
        out.append(f"| {ls} | {_ids_compact(per_line[ls])} |")
    out.append("")
    return "\n".join(out)


def _ids_compact(ids: list[str]) -> str:
    return ", ".join(ids) if len(ids) <= 8 else f"{ids[0]} … {ids[-1]} ({len(ids)})"


def cmd_coverage(args) -> int:
    data, slices, plan = get_inputs()
    errs = validate_data(data, slices, plan)
    if errs:
        print("ОШИБКА: реестр не проходит проверку, покрытие не строится; запустите validate", file=sys.stderr)
        return 1
    text = render_coverage(data, plan)
    if args.write:
        p = ROOT / args.write
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"записано: {args.write}")
        return 0
    if args.check:
        p = ROOT / args.check
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            print(f"УСТАРЕЛО: {args.check} не соответствует реестру; выполните coverage --write")
            return 1
        print(f"OK: {args.check} соответствует реестру")
        return 0
    print(text)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Реестр требований")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    sub.add_parser("report")
    u = sub.add_parser("unverified")
    u.add_argument("--slice")
    c = sub.add_parser("coverage")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--write")
    g.add_argument("--check")
    args = ap.parse_args(argv)
    return {"validate": cmd_validate, "report": cmd_report,
            "unverified": cmd_unverified, "coverage": cmd_coverage}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
