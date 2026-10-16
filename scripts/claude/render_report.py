#!/usr/bin/env python3
"""Собирает отчёт проверки изоляции из результатов `verify_isolation.sh` (R-119).

  python3 scripts/claude/render_report.py РЕЗУЛЬТАТЫ.jsonl ОТЧЁТ.md [--template ШАБЛОН.md]

Строка результатов: {"id": "...", "status": "PASS|FAIL|SKIP", "detail": "..."}. Отчёт заполняется машинно по
шаблону. Итог «ПРОЙДЕНА» только если выполнены **все** обязательные проверки: пропущенная обязательная считается
проваленной. Токен и другие секреты в отчёт не попадают: детали проходят через сканер секретов.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from iam.backup.scan import REDACTED, SecretScanner   # noqa: E402

DEFAULT_TEMPLATE = ROOT / "docs" / "evidence" / "claude-isolation.template.md"

# Обязательные проверки: id → что доказывает (условие перехода PLAN §3, R-110…R-115, R-119).
REQUIRED = {
    "arch": "контейнер linux/arm64 (R-110)",
    "non_root": "процесс не root (R-110, R-114)",
    "no_mounts": "ни одного монтирования с хоста: домашних папок, базы, репозитория нет (R-111)",
    "canary_file": "контрольный файл хоста недоступен внутри (R-111)",
    "canary_env": "контрольные секреты окружения (токен Telegram и др.) недоступны внутри (R-111)",
    "no_docker_socket": "Docker socket в контейнере нет (R-112)",
    "read_only_root": "корневая файловая система только для чтения (R-114)",
    "no_capabilities": "все capabilities сняты, no-new-privileges (R-114)",
    "limits": "лимиты процессов и памяти применены (R-114)",
    "net_allowed": "разрешённый адрес через посредника доступен (R-115)",
    "net_denied": "запрещённый адрес через посредника недоступен (R-115)",
    "net_direct_denied": "прямой выход в сеть в обход посредника невозможен (R-115)",
    "auth": "вход по подписке работает внутри контейнера (R-113, R-119)",
    "format": "ответ в нужном структурированном формате (R-119)",
    "no_tools": "модель не получила инструментов: нет разрешений/попыток (R-117, R-118)",
}


def read_results(path: Path) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            if row.get("status") not in ("PASS", "FAIL", "SKIP") or "id" not in row:
                raise ValueError(f"{path}:{n}: нужна строка вида {{id, status: PASS|FAIL|SKIP, detail}}")
            found[row["id"]] = row
    return found


def verdict(results: dict[str, dict]) -> tuple[bool, list[str]]:
    """(пройдена ли проверка, список проблем)."""
    problems = [f"{i}: нет результата" for i in REQUIRED if i not in results]
    problems += [f"{i}: {results[i]['status']}" for i in REQUIRED if i in results and results[i]["status"] != "PASS"]
    problems += [f"{i}: FAIL (не обязательная)" for i, r in results.items() if i not in REQUIRED and r["status"] == "FAIL"]
    return not problems, problems


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()[:300]


def render(template: str, results: dict[str, dict], *, when: str, host: str,
           scanner: SecretScanner | None = None) -> str:
    scanner = scanner or SecretScanner()
    ok, problems = verdict(results)
    rows = []
    for i, what in REQUIRED.items():
        r = results.get(i, {"status": "НЕТ", "detail": ""})
        rows.append(f"| `{i}` | {_cell(what)} | {r['status']} | {_cell(r.get('detail', ''))} |")
    for i, r in results.items():
        if i not in REQUIRED:
            rows.append(f"| `{_cell(i)}` | дополнительно | {r['status']} | {_cell(r.get('detail', ''))} |")
    body = (template.replace("{{DATE}}", when).replace("{{HOST}}", _cell(host))
            .replace("{{VERDICT}}", "ПРОЙДЕНА" if ok else "НЕ ПРОЙДЕНА")
            .replace("{{PROBLEMS}}", "\n".join(f"- {_cell(p)}" for p in problems) or "- нет")
            .replace("{{ROWS}}", "\n".join(rows)))
    clean, _ = scanner.redact(body)
    return clean


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results")
    ap.add_argument("report")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    args = ap.parse_args(argv)
    results = read_results(Path(args.results))
    text = render(Path(args.template).read_text(encoding="utf-8"), results,
                  when=dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%MZ"), host=platform.platform())
    Path(args.report).write_text(text, encoding="utf-8")
    ok, problems = verdict(results)
    print(("ПРОЙДЕНА" if ok else "НЕ ПРОЙДЕНА") + (": " + "; ".join(problems) if problems else ""))
    if REDACTED in text:
        print("внимание: в отчёте были найдены и удалены секреты", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
