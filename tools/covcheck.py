#!/usr/bin/env python3
"""Измеритель покрытия тестами по модулям (D-009, D-011).

Прогоняет все тесты под `coverage.py` (версия зафиксирована в pyproject.toml, `[project.optional-dependencies]
dev`) и проверяет пороги из `[tool.iam.coverage]`: каждый модуль `src/` и `tools/` — не ниже `min_lines` %
строк; критичные модули (список шаблонов `critical`) — не ниже `critical_min_lines` % строк и
`critical_min_branches` % ветвей. Порог — по каждому модулю, а не по среднему. Модуль, который тесты ни
разу не загрузили, считается покрытым на 0 %. Метка исключения из замера (PRAGMA ниже) запрещена:
исключать код из замера нельзя.

  python3 tools/covcheck.py                 прогнать тесты и проверить пороги
  python3 tools/covcheck.py --json FILE     проверить готовый отчёт `coverage json` (без прогона)

Код возврата: 0 — пороги выполнены; 1 — нет (или тесты упали); 2 — нет coverage.py / ошибка настройки.
"""
from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(os.environ.get("IAM_ROOT") or Path(__file__).resolve().parent.parent).resolve()
MEASURED_DIRS = ("src", "tools")
PRAGMA = "pragma: " + "no cover"  # образец метки; собран из частей, чтобы этот файл сам не попадал под запрет
# Тот же шаблон, которым coverage.py находит метку по умолчанию (любой регистр, с двоеточием или без).
PRAGMA_RE = re.compile(r"#\s*(pragma|PRAGMA)[:\s]?\s*(no|NO)\s*(cover|COVER)", re.IGNORECASE)
# Единственное разрешённое исключение из замера (pyproject.toml, [tool.coverage.report] exclude_also).
ALLOWED_EXCLUDES = ["if __name__ == .__main__.:"]
INSTALL_HINT = "pip install 'coverage==7.10.6'  (или: pip install -e '.[dev]')"


@dataclass(frozen=True)
class Config:
    min_lines: float
    critical_min_lines: float
    critical_min_branches: float
    critical: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    path: str
    lines_pct: float
    branches_pct: float
    critical: bool
    problems: tuple[str, ...]
    missing: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def exclusion_problems(pyproject: Path) -> list[str]:
    """Настройки coverage не должны исключать из замера ничего, кроме разрешённого."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    cov = data.get("tool", {}).get("coverage", {})
    probs = []
    report = cov.get("report", {})
    if report.get("exclude_also", ALLOWED_EXCLUDES) != ALLOWED_EXCLUDES:
        probs.append(f"exclude_also расширен: {report['exclude_also']!r}; разрешено только {ALLOWED_EXCLUDES!r}")
    for key in ("exclude_lines", "partial_branches", "partial_also", "omit"):
        if key in report:
            probs.append(f"[tool.coverage.report] {key} запрещён")
    if "omit" in cov.get("run", {}):
        probs.append("[tool.coverage.run] omit запрещён")
    return probs


def load_config(pyproject: Path) -> Config:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    try:
        c = data["tool"]["iam"]["coverage"]
        return Config(float(c["min_lines"]), float(c["critical_min_lines"]), float(c["critical_min_branches"]),
                      tuple(c["critical"]))
    except KeyError as e:
        raise SystemExit(f"ОШИБКА: в {pyproject} нет [tool.iam.coverage] или поля {e}") from None


def measured_files(root: Path) -> list[str]:
    out = []
    for d in MEASURED_DIRS:
        out += [p.relative_to(root).as_posix() for p in sorted((root / d).rglob("*.py"))]
    return out


def _pct(part: int, whole: int) -> float:
    return 100.0 if whole == 0 else 100.0 * part / whole


def evaluate(report: dict, cfg: Config, root: Path) -> list[Result]:
    """Проверить отчёт `coverage json` против порогов. Возвращает результат по каждому модулю."""
    files = {}
    for name, info in report.get("files", {}).items():
        p = Path(name)
        rel = p.relative_to(root).as_posix() if p.is_absolute() else p.as_posix()
        files[rel] = info
    results = []
    for rel in measured_files(root):
        critical = any(fnmatch.fnmatch(rel, pat) for pat in cfg.critical)
        problems = []
        if PRAGMA_RE.search((root / rel).read_text(encoding="utf-8")):
            problems.append("метка исключения из замера запрещена")
        info = files.get(rel)
        if info is None:
            results.append(Result(rel, 0.0, 0.0, critical, tuple(problems + ["тесты ни разу не загрузили модуль"])))
            continue
        s = info["summary"]
        lines = _pct(s["covered_lines"], s["num_statements"])
        branches = _pct(s.get("covered_branches", 0), s.get("num_branches", 0))
        need_lines = cfg.critical_min_lines if critical else cfg.min_lines
        if lines < need_lines:
            problems.append(f"строк {lines:.1f} % < {need_lines:g} %")
        if critical and branches < cfg.critical_min_branches:
            problems.append(f"ветвей {branches:.1f} % < {cfg.critical_min_branches:g} %")
        results.append(Result(rel, lines, branches, critical, tuple(problems), tuple(info.get("missing_lines", []))))
    return results


def render(results: list[Result]) -> str:
    rows = [f"{'модуль':<44} {'строки':>8} {'ветви':>8}  итог"]
    for r in results:
        mark = "ok" if r.ok else "НИЖЕ ПОРОГА: " + "; ".join(r.problems)
        crit = " [критичный]" if r.critical else ""
        rows.append(f"{r.path:<44} {r.lines_pct:7.1f}% {r.branches_pct:7.1f}%  {mark}{crit}")
        if not r.ok and r.missing:
            rows.append(f"{'':<44} не покрыты строки: {', '.join(map(str, r.missing[:30]))}")
    bad = sum(not r.ok for r in results)
    rows.append(f"Модулей: {len(results)}, ниже порога: {bad}.")
    return "\n".join(rows)


def run_tests_under_coverage(root: Path, out_json: Path) -> int:
    rc = str(root / "pyproject.toml")
    cov = [sys.executable, "-m", "coverage"]
    # Если сам covcheck запущен под чужим замером (его тесты), внешний замер не должен вмешиваться.
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_PROCESS_")}
    run = lambda cmd, *a, **kw: subprocess.run([*cov, cmd, f"--rcfile={rc}", *a], cwd=root, env=env, **kw)  # noqa: E731
    run("erase", check=True)
    tests = run("run", "-p", "-m", "unittest", "discover", "-s", "tests", "-q")
    run("combine", "-q", check=True)
    run("json", "-q", "-o", str(out_json), check=True)
    return tests.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Покрытие тестами по модулям (D-009)")
    ap.add_argument("--json", type=Path, help="готовый отчёт coverage json вместо прогона тестов")
    args = ap.parse_args(argv)
    cfg = load_config(ROOT / "pyproject.toml")
    tests_rc = 0
    if args.json is not None:
        report = json.loads(args.json.read_text(encoding="utf-8"))
    else:
        if importlib.util.find_spec("coverage") is None:
            print(f"ОШИБКА: не установлен coverage.py. Установите: {INSTALL_HINT}", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "coverage.json"
            tests_rc = run_tests_under_coverage(ROOT, out)
            report = json.loads(out.read_text(encoding="utf-8"))
    results = evaluate(report, cfg, ROOT)
    print(render(results))
    excl = exclusion_problems(ROOT / "pyproject.toml")
    for prob in excl:
        print(f"ОШИБКА настройки замера: {prob}")
    if tests_rc != 0:
        print("Тесты упали — замер недействителен.")
        return 1
    return 0 if all(r.ok for r in results) and not excl else 1


if __name__ == "__main__":
    sys.exit(main())
