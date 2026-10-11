#!/usr/bin/env python3
"""Память проекта между сессиями: состояние срезов, журнал, барьеры.

Единственный допустимый способ менять docs/state.json. Файл docs/STATE.md
генерируется отсюда и вручную не правится. Только стандартная библиотека.

Команды:
  brief                      сводка для старта сессии
  start SLICE                начать срез (проверяет зависимости)
  finish SLICE -m "итог"     закрыть срез, выбрать следующий
  block SLICE -m "причина"   пометить срез заблокированным
  unblock SLICE              вернуть заблокированный срез в работу
  gate-add SLICE -m "текст"  добавить барьер (нужно действие пользователя)
  gate-resolve ID -m "ответ" закрыть барьер
  assume ID -m "текст"       добавить/обновить допущение (--status ...)
  log -m "текст"             запись в журнал
  render                     пересоздать docs/STATE.md
  check                      проверка целостности памяти
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("IAM_ROOT") or Path(__file__).resolve().parent.parent)
DOCS = ROOT / "docs"
STATE_JSON = DOCS / "state.json"
STATE_MD = DOCS / "STATE.md"
JOURNAL = DOCS / "JOURNAL.md"
SLICES_DIR = DOCS / "slices"

STATUSES = ("todo", "in_progress", "done", "blocked")
REQUIRED_HEADINGS = (
    "## Простыми словами",
    "## Что делаем",
    "## Границы файлов",
    "## Проверки",
    "## Итог",
)
MARK = {"todo": "[ ]", "in_progress": "[~]", "done": "[x]", "blocked": "[!]"}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def load() -> dict:
    return json.loads(STATE_JSON.read_text(encoding="utf-8"))


def save(state: dict) -> None:
    tmp = STATE_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_JSON)
    STATE_MD.write_text(render(state), encoding="utf-8")


def slice_file(sid: str) -> Path | None:
    found = sorted(SLICES_DIR.glob(f"{sid}-*.md"))
    return found[0] if found else None


def next_slice(state: dict) -> str | None:
    """Первый по порядку срез, готовый к работе (in_progress приоритетнее)."""
    sl = state["slices"]
    for sid, s in sl.items():
        if s["status"] == "in_progress":
            return sid
    for sid, s in sl.items():
        if s["status"] == "todo" and all(sl[d]["status"] == "done" for d in s["depends"]):
            return sid
    return None


def journal(title: str, body: str) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    if not JOURNAL.exists():
        JOURNAL.write_text(
            "# Журнал проекта\n\nДописывается только в конец. Правки задним числом запрещены.\n\n",
            encoding="utf-8",
        )
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(f"## {now()} — {title}\n\n{body.strip()}\n\n")


def git(*args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=15)
        return r.stdout.strip()
    except Exception:
        return ""


def render(state: dict) -> str:
    sl = state["slices"]
    out = [
        "<!-- СГЕНЕРИРОВАНО tools/memory.py из docs/state.json. Вручную не править. -->",
        "",
        f"# Состояние проекта: {state['project']}",
        "",
        f"Текущий срез: **{state['current_slice'] or '— (все закрыты)'}**",
        "",
        "| | Срез | Название | Этап | Зависит от |",
        "|---|---|---|---|---|",
    ]
    for sid, s in sl.items():
        out.append(f"| {MARK[s['status']]} | {sid} | {s['title']} | {s['stage']} | {', '.join(s['depends']) or '—'} |")
    done = sum(1 for s in sl.values() if s["status"] == "done")
    out += ["", f"Закрыто срезов: {done} из {len(sl)}.", ""]
    out += ["## Барьеры (нужно действие пользователя)", ""]
    for g in state["gates"]:
        out.append(f"- **{g['id']}** ({g['slice']}, {g['status']}): {g['text']}"
                   + (f" → ответ: {g['answer']}" if g.get("answer") else ""))
    out += ["", "## Допущения", ""]
    for a in state["assumptions"]:
        out.append(f"- **{a['id']}** ({a['status']}): {a['text']}")
    out += ["", "Журнал: docs/JOURNAL.md · Решения: docs/DECISIONS.md · План: docs/PLAN.md", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- команды


def cmd_brief(args, state):
    sid = state["current_slice"]
    print(f"== {state['project']} ==")
    print(f"ветка: {git('rev-parse', '--abbrev-ref', 'HEAD') or '?'}; "
          f"несохранённых файлов: {len(git('status', '--short').splitlines())}")
    sl = state["slices"]
    done = [k for k, v in sl.items() if v["status"] == "done"]
    print(f"закрыто срезов: {len(done)}/{len(sl)} ({', '.join(done[-3:]) or 'нет'})")
    if sid:
        s = sl[sid]
        print(f"\nТЕКУЩИЙ СРЕЗ: {sid} — {s['title']} [{s['status']}]")
        f = slice_file(sid)
        print(f"описание: {f.relative_to(ROOT) if f else 'ФАЙЛ НЕ НАЙДЕН'}")
        unmet = [d for d in s["depends"] if sl[d]["status"] != "done"]
        if unmet:
            print(f"!! не закрыты зависимости: {unmet}")
    else:
        print("\nВсе срезы закрыты.")
    gates = [g for g in state["gates"] if g["status"] == "open" and sl[g["slice"]]["status"] != "done"]
    near = [g for g in gates if sid and g["slice"] == sid]
    if near:
        print("\nБАРЬЕРЫ ТЕКУЩЕГО СРЕЗА (нужен пользователь):")
        for g in near:
            print(f"  {g['id']}: {g['text']}")
    pend = [a for a in state["assumptions"] if a["status"] == "pending-confirm"]
    if pend:
        print("\nДопущения, ждущие подтверждения:")
        for a in pend:
            print(f"  {a['id']}: {a['text']}")
    if JOURNAL.exists():
        heads = [l for l in JOURNAL.read_text(encoding="utf-8").splitlines() if l.startswith("## ")]
        print("\nПоследние записи журнала:")
        for h in heads[-5:]:
            print("  " + h[3:])
    print("\nПротокол: CLAUDE.md. Дальше: прочитать описание среза и начать работу.")


def cmd_start(args, state):
    sl = state["slices"]
    s = sl[args.slice]
    unmet = [d for d in s["depends"] if sl[d]["status"] != "done"]
    if unmet and not args.force:
        sys.exit(f"Нельзя начать {args.slice}: не закрыты {unmet} (--force, только с записью в DECISIONS.md)")
    if s["status"] == "done":
        sys.exit(f"{args.slice} уже закрыт")
    s["status"] = "in_progress"
    s["started"] = s.get("started") or now()
    state["current_slice"] = args.slice
    save(state)
    journal(f"{args.slice} — старт", s["title"])
    print(f"{args.slice} начат")


def cmd_finish(args, state):
    sl = state["slices"]
    s = sl[args.slice]
    if s["status"] != "in_progress":
        sys.exit(f"{args.slice} не в работе (статус {s['status']})")
    f = slice_file(args.slice)
    text = f.read_text(encoding="utf-8") if f else ""
    if "ИТОГ-НЕ-ЗАПОЛНЕН" in text:
        sys.exit(f"В {f.name} не заполнен раздел «Итог» (маркер ИТОГ-НЕ-ЗАПОЛНЕН)")
    s["status"] = "done"
    s["finished"] = now()
    s["summary"] = args.message
    state["current_slice"] = next_slice(state)
    save(state)
    journal(f"{args.slice} — закрыт", args.message)
    print(f"{args.slice} закрыт. Следующий: {state['current_slice']}")


def cmd_block(args, state):
    s = state["slices"][args.slice]
    s["status"] = "blocked"
    s["blocked_reason"] = args.message
    state["current_slice"] = next_slice(state)
    save(state)
    journal(f"{args.slice} — заблокирован", args.message)


def cmd_unblock(args, state):
    s = state["slices"][args.slice]
    s["status"] = "in_progress"
    s.pop("blocked_reason", None)
    state["current_slice"] = args.slice
    save(state)
    journal(f"{args.slice} — разблокирован", "возобновлён")


def cmd_gate_add(args, state):
    n = 1 + max([int(g["id"][1:]) for g in state["gates"]] or [0])
    gid = f"G{n:02d}"
    state["gates"].append({"id": gid, "slice": args.slice, "status": "open", "text": args.message})
    save(state)
    journal(f"барьер {gid}", f"{args.slice}: {args.message}")
    print(gid)


def cmd_gate_resolve(args, state):
    for g in state["gates"]:
        if g["id"] == args.slice:
            g["status"] = "resolved"
            g["answer"] = args.message
            g["resolved"] = now()
            save(state)
            journal(f"барьер {g['id']} закрыт", args.message)
            return
    sys.exit("нет такого барьера")


def cmd_assume(args, state):
    for a in state["assumptions"]:
        if a["id"] == args.slice:
            a["text"] = args.message
            a["status"] = args.status or a["status"]
            break
    else:
        state["assumptions"].append({"id": args.slice, "status": args.status or "active", "text": args.message})
    save(state)
    journal(f"допущение {args.slice}", args.message)


def cmd_log(args, state):
    journal("заметка", args.message)


def cmd_render(args, state):
    STATE_MD.write_text(render(state), encoding="utf-8")


def problems(state: dict) -> list[str]:
    p: list[str] = []
    sl = state["slices"]
    if sum(1 for s in sl.values() if s["status"] == "in_progress") > 1:
        p.append("больше одного среза в работе")
    cur = state["current_slice"]
    if cur is not None and cur not in sl:
        p.append(f"current_slice={cur} не существует")
    for sid, s in sl.items():
        if s["status"] not in STATUSES:
            p.append(f"{sid}: неизвестный статус {s['status']}")
        for d in s["depends"]:
            if d not in sl:
                p.append(f"{sid}: зависимость {d} не существует")
            elif list(sl).index(d) > list(sl).index(sid):
                p.append(f"{sid}: зависимость {d} стоит позже по порядку")
        f = slice_file(sid)
        if not f:
            p.append(f"{sid}: нет файла docs/slices/{sid}-*.md")
            continue
        text = f.read_text(encoding="utf-8")
        for h in REQUIRED_HEADINGS:
            if h not in text:
                p.append(f"{f.name}: нет раздела «{h}»")
        if s["status"] == "done" and "ИТОГ-НЕ-ЗАПОЛНЕН" in text:
            p.append(f"{sid}: закрыт, но «Итог» не заполнен")
        if s["status"] == "done" and not s.get("finished"):
            p.append(f"{sid}: закрыт без даты")
    for g in state["gates"]:
        if g["slice"] not in sl:
            p.append(f"барьер {g['id']}: срез {g['slice']} не существует")
    if STATE_MD.exists() and STATE_MD.read_text(encoding="utf-8") != render(state):
        p.append("docs/STATE.md не соответствует state.json (запустите render)")
    return p


def cmd_check(args, state):
    p = problems(state)
    for x in p:
        print("ОШИБКА:", x)
    if p:
        sys.exit(1)
    print("память проекта согласована")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, has_slice, has_msg in [
        ("brief", cmd_brief, False, False), ("start", cmd_start, True, False),
        ("finish", cmd_finish, True, True), ("block", cmd_block, True, True),
        ("unblock", cmd_unblock, True, False), ("gate-add", cmd_gate_add, True, True),
        ("gate-resolve", cmd_gate_resolve, True, True), ("assume", cmd_assume, True, True),
        ("log", cmd_log, False, True), ("render", cmd_render, False, False),
        ("check", cmd_check, False, False),
    ]:
        sp = sub.add_parser(name)
        if has_slice:
            sp.add_argument("slice", help="ID среза (для gate-resolve — ID барьера, для assume — ID допущения)")
        if has_msg:
            sp.add_argument("-m", "--message", required=True)
        if name == "start":
            sp.add_argument("--force", action="store_true")
        if name == "assume":
            sp.add_argument("--status")
        sp.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    state = load()
    if hasattr(args, "slice") and args.cmd not in ("gate-resolve", "assume") and args.slice not in state["slices"]:
        sys.exit(f"нет среза {args.slice}")
    args.fn(args, state)


if __name__ == "__main__":
    main()
