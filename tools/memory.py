#!/usr/bin/env python3
"""Память проекта между сессиями: состояние срезов, журнал, барьеры.

Единственный допустимый способ менять docs/state.json. Файл docs/STATE.md
генерируется отсюда и вручную не правится. Только стандартная библиотека.

Команды:
  brief                      сводка для старта сессии
  sync                       найти самую свежую память среди веток origin/*
  start SLICE                начать срез (проверяет зависимости и барьеры)
  finish SLICE -m "итог"     закрыть срез (проверяет зависимости, барьеры, «Итог», целостность)
  block SLICE -m "причина"   пометить срез заблокированным
  unblock SLICE              вернуть заблокированный срез в работу
  gate-add SLICE -m "текст"  добавить барьер (--blocks-start S.. --blocks-finish S..)
  gate-resolve ID -m "ответ" закрыть барьер
  assume ID -m "текст"       добавить/обновить допущение (--status active|pending-confirm|confirmed|rejected)
  log -m "текст"             запись в журнал
  checkpoint -m "текст"      журнал + git add -A + коммит + push (страховка от обрыва сессии)
  render                     пересоздать docs/STATE.md
  check                      проверка целостности памяти
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("IAM_ROOT") or Path(__file__).resolve().parent.parent).resolve()
DOCS = ROOT / "docs"
STATE_JSON = DOCS / "state.json"
STATE_MD = DOCS / "STATE.md"
JOURNAL = DOCS / "JOURNAL.md"
SLICES_DIR = DOCS / "slices"
LOCK = DOCS / ".memory.lock"

STATUSES = ("todo", "in_progress", "done", "blocked")
ASSUME_STATUSES = ("active", "pending-confirm", "confirmed", "rejected")
REQUIRED_HEADINGS = (
    "## Простыми словами",
    "## Что делаем",
    "## Границы файлов",
    "## Проверки",
    "## Итог",
)
SUMMARY_MARKER = "ИТОГ-НЕ-ЗАПОЛНЕН"
MARK = {"todo": "[ ]", "in_progress": "[~]", "done": "[x]", "blocked": "[!]"}


def die(msg: str) -> "None":
    sys.exit(f"ОШИБКА: {msg}")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def load() -> dict:
    try:
        return json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        die(f"не удалось прочитать {STATE_JSON}: {e}")


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save(state: dict) -> None:
    state["revision"] = state.get("revision", 0) + 1
    atomic_write(STATE_JSON, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    atomic_write(STATE_MD, render(state))


@contextlib.contextmanager
def locked():
    """Взаимное исключение для команд, меняющих память (параллельные запуски)."""
    DOCS.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def slice_file(sid: str) -> Path | None:
    found = sorted(SLICES_DIR.glob(f"{sid}-*.md"))
    return found[0] if found else None


def summary_body(text: str) -> str | None:
    """Тело раздела «## Итог» (точный заголовок), без маркера; None если раздела нет."""
    m = re.search(r"^## Итог[ \t]*$", text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return (rest[: nxt.start()] if nxt else rest).strip()


def next_slice(state: dict) -> str | None:
    """Срез в работе, иначе первый по порядку todo с закрытыми зависимостями."""
    sl = state["slices"]
    for sid, s in sl.items():
        if s["status"] == "in_progress":
            return sid
    for sid, s in sl.items():
        if s["status"] == "todo" and all(sl[d]["status"] == "done" for d in s["depends"]):
            return sid
    return None


def open_gates(state: dict, field: str, sid: str) -> list[dict]:
    return [g for g in state["gates"] if g["status"] == "open" and sid in g.get(field, [])]


def journal(title: str, body: str) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    if not JOURNAL.exists():
        atomic_write(
            JOURNAL,
            "# Журнал проекта\n\nДописывается только в конец. Правки задним числом запрещены.\n\n",
        )
    # заголовки внутри тела экранируем, чтобы не ломать разбор списка записей
    safe = "\n".join(("\\" + l) if l.startswith("## ") else l for l in body.strip().splitlines())
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(f"## {now()} — {title}\n\n{safe}\n\n")


def git(*args: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def current_branch() -> str:
    # symbolic-ref работает и в репозитории без коммитов (там падает rev-parse)
    return git("symbolic-ref", "--short", "-q", "HEAD")


def render(state: dict) -> str:
    sl = state["slices"]
    cur = state["current_slice"]
    out = [
        "<!-- СГЕНЕРИРОВАНО tools/memory.py из docs/state.json. Вручную не править. -->",
        "",
        f"# Состояние проекта: {state['project']}",
        "",
        f"Ревизия памяти: {state.get('revision', 0)}",
        f"Текущий срез: **{cur or ('— (нет доступных срезов; есть заблокированные)' if any(s['status'] == 'blocked' for s in sl.values()) else '— (все закрыты)')}**",
        "",
        "| | Срез | Название | Этап | Зависит от |",
        "|---|---|---|---|---|",
    ]
    for sid, s in sl.items():
        out.append(f"| {MARK[s['status']]} | {sid} | {s['title']} | {s['stage']} | {', '.join(s['depends']) or '—'} |")
    done = sum(1 for s in sl.values() if s["status"] == "done")
    out += ["", f"Закрыто срезов: {done} из {len(sl)}.", ""]
    blocked = [(k, s) for k, s in sl.items() if s["status"] == "blocked"]
    if blocked:
        out += ["## Заблокированные срезы", ""]
        out += [f"- **{k}**: {s.get('blocked_reason', '?')}" for k, s in blocked]
        out.append("")
    out += ["## Барьеры (нужно действие пользователя)", ""]
    for g in state["gates"]:
        extra = []
        if g.get("blocks_start"):
            extra.append("не даёт начать " + ", ".join(g["blocks_start"]))
        if g.get("blocks_finish"):
            extra.append("не даёт закрыть " + ", ".join(g["blocks_finish"]))
        out.append(f"- **{g['id']}** ({g['slice']}, {g['status']}"
                   + (f"; {'; '.join(extra)}" if extra else "") + f"): {g.get('text', '')}"
                   + (f" → ответ: {g['answer']}" if g.get("answer") else ""))
    out += ["", "## Допущения", ""]
    for a in state["assumptions"]:
        out.append(f"- **{a['id']}** ({a['status']}): {a['text']}")
    out += ["", "Журнал: docs/JOURNAL.md · Решения: docs/DECISIONS.md · План: docs/PLAN.md", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- команды


def cmd_brief(args, state):
    sid = state["current_slice"]
    sl = state["slices"]
    print(f"== {state['project']} == (ревизия памяти {state.get('revision', 0)})")
    print(f"ветка: {current_branch() or '?'}; "
          f"несохранённых файлов: {len(git('status', '--short').splitlines())}")
    done = [k for k, v in sl.items() if v["status"] == "done"]
    print(f"закрыто срезов: {len(done)}/{len(sl)} (последние: {', '.join(done[-3:]) or 'нет'})")
    if sid:
        s = sl[sid]
        print(f"\nТЕКУЩИЙ СРЕЗ: {sid} — {s['title']} [{s['status']}]")
        f = slice_file(sid)
        print(f"описание: {f.relative_to(ROOT) if f else 'ФАЙЛ НЕ НАЙДЕН'}")
    else:
        blocked = [(k, v) for k, v in sl.items() if v["status"] == "blocked"]
        if blocked:
            print("\nТЕКУЩЕГО СРЕЗА НЕТ: работа заблокирована.")
            for k, v in blocked:
                print(f"  {k}: {v.get('blocked_reason', '?')}")
        else:
            print("\nВсе срезы закрыты.")
    gates = [g for g in state["gates"] if g["status"] == "open"]
    if gates:
        print("\nОТКРЫТЫЕ БАРЬЕРЫ (нужны действия пользователя):")
        for g in gates:
            tag = []
            if sl[g["slice"]]["status"] == "done":
                tag.append("ДОЛГ: срез уже закрыт без этого")
            if g.get("blocks_start"):
                tag.append("не даёт начать " + ",".join(g["blocks_start"]))
            if g.get("blocks_finish"):
                tag.append("не даёт закрыть " + ",".join(g["blocks_finish"]))
            print(f"  {g['id']} [{g['slice']}] {g.get('text', '')}" + (f"  <{'; '.join(tag)}>" if tag else ""))
    pend = [a for a in state["assumptions"] if a["status"] == "pending-confirm"]
    if pend:
        print("\nДопущения, ждущие подтверждения:")
        for a in pend:
            print(f"  {a['id']}: {a['text']}")
    if JOURNAL.exists():
        heads = [l for l in JOURNAL.read_text(encoding="utf-8").splitlines() if re.match(r"^## \d{4}-", l)]
        print("\nПоследние записи журнала:")
        for h in heads[-5:]:
            print("  " + h[3:])
    p = problems(state)
    if p:
        print("\n!! ПАМЯТЬ НЕСОГЛАСОВАНА:")
        for x in p:
            print("  -", x)
    print("\nПротокол: CLAUDE.md. Если память может быть устаревшей — `python3 tools/memory.py sync`.")


def cmd_sync(args, state):
    git("fetch", "origin", timeout=60)
    refs = [r for r in git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin").splitlines()
            if r not in ("origin", "origin/HEAD")]
    here = state.get("revision", 0)
    branch = current_branch()
    print(f"локальная ревизия памяти: {here} (ветка {branch})")
    best = (here, None)
    for ref in refs:
        try:
            rev = json.loads(git("show", f"{ref}:docs/state.json")).get("revision", 0)
        except ValueError:
            continue
        print(f"  {ref}: ревизия {rev}")
        if rev > best[0]:
            best = (rev, ref)
    if best[1]:
        print(f"\nПАМЯТЬ ЛОКАЛЬНО УСТАРЕЛА. Свежее в {best[1]} (ревизия {best[0]}).")
        print(f"Если нет несохранённых правок: git checkout -B {branch} {best[1]}")
    else:
        print("\nЛокальная память самая свежая (или других веток нет).")


def cmd_start(args, state):
    sl = state["slices"]
    s = sl[args.slice]
    if s["status"] == "done":
        die(f"{args.slice} уже закрыт")
    other = [k for k, v in sl.items() if v["status"] == "in_progress" and k != args.slice]
    if other:
        die(f"уже в работе {other}; закройте или заблокируйте его")
    unmet = [d for d in s["depends"] if sl[d]["status"] != "done"]
    if unmet and not args.force:
        die(f"нельзя начать {args.slice}: не закрыты {unmet} (--force, только с записью в DECISIONS.md)")
    gates = open_gates(state, "blocks_start", args.slice)
    if gates and not args.force:
        die(f"{args.slice} не начать: открыты барьеры {[g['id'] for g in gates]} (нужен пользователь)")
    s["status"] = "in_progress"
    s["started"] = s.get("started") or now()
    s.pop("blocked_reason", None)
    state["current_slice"] = args.slice
    save(state)
    journal(f"{args.slice} — старт", s["title"])
    print(f"{args.slice} начат")


def cmd_finish(args, state):
    sl = state["slices"]
    s = sl[args.slice]
    if s["status"] != "in_progress":
        die(f"{args.slice} не в работе (статус {s['status']})")
    unmet = [d for d in s["depends"] if sl[d]["status"] != "done"]
    if unmet:
        die(f"нельзя закрыть {args.slice}: не закрыты зависимости {unmet}")
    gates = open_gates(state, "blocks_finish", args.slice)
    if gates:
        die(f"{args.slice} не закрыть: открыты барьеры {[g['id'] for g in gates]} (нужен пользователь)")
    f = slice_file(args.slice)
    if not f:
        die(f"нет файла описания среза {args.slice}")
    text = f.read_text(encoding="utf-8")
    body = summary_body(text)
    if not body or SUMMARY_MARKER in text:
        die(f"в {f.name} не заполнен раздел «Итог» (пусто или остался маркер {SUMMARY_MARKER})")
    s["status"] = "done"
    s["finished"] = now()
    s["summary"] = args.message
    state["current_slice"] = next_slice(state)
    bad = problems(state, skip_render=True)
    if bad:
        die("после закрытия память была бы несогласована:\n  - " + "\n  - ".join(bad))
    save(state)
    journal(f"{args.slice} — закрыт", args.message)
    print(f"{args.slice} закрыт. Следующий: {state['current_slice']}")


def cmd_block(args, state):
    s = state["slices"][args.slice]
    if s["status"] not in ("todo", "in_progress"):
        die(f"{args.slice} нельзя заблокировать из статуса {s['status']}")
    s["status"] = "blocked"
    s["blocked_reason"] = args.message
    state["current_slice"] = next_slice(state)
    save(state)
    journal(f"{args.slice} — заблокирован", args.message)


def cmd_unblock(args, state):
    sl = state["slices"]
    s = sl[args.slice]
    if s["status"] != "blocked":
        die(f"{args.slice} не заблокирован (статус {s['status']})")
    other = [k for k, v in sl.items() if v["status"] == "in_progress"]
    if other:
        die(f"уже в работе {other}")
    s["status"] = "in_progress"
    s.pop("blocked_reason", None)
    state["current_slice"] = args.slice
    save(state)
    journal(f"{args.slice} — разблокирован", "возобновлён")


def cmd_gate_add(args, state):
    sl = state["slices"]
    for k in (args.blocks_start or []) + (args.blocks_finish or []):
        if k not in sl:
            die(f"нет среза {k}")
    n = 1 + max([int(g["id"][1:]) for g in state["gates"]] or [0])
    g = {"id": f"G{n:02d}", "slice": args.slice, "status": "open", "text": args.message}
    if args.blocks_start:
        g["blocks_start"] = args.blocks_start
    if args.blocks_finish:
        g["blocks_finish"] = args.blocks_finish
    state["gates"].append(g)
    save(state)
    journal(f"барьер {g['id']}", f"{args.slice}: {args.message}")
    print(g["id"])


def cmd_gate_resolve(args, state):
    for g in state["gates"]:
        if g["id"] == args.slice:
            if g["status"] == "resolved":
                die(f"барьер {g['id']} уже закрыт")
            g["status"] = "resolved"
            g["answer"] = args.message
            g["resolved"] = now()
            save(state)
            journal(f"барьер {g['id']} закрыт", args.message)
            return
    die("нет такого барьера")


def cmd_assume(args, state):
    if args.status and args.status not in ASSUME_STATUSES:
        die(f"статус допущения из {ASSUME_STATUSES}")
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


def cmd_checkpoint(args, state):
    journal("контрольная точка", args.message)
    branch = current_branch()
    if not branch:
        die("нет текущей ветки для коммита")
    git("add", "-A")
    r = subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "-m", f"WIP: {args.message}"],
                       capture_output=True, text=True)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        die(f"commit не удался: {r.stdout}{r.stderr}")
    delay = 2
    attempts = int(os.environ.get("IAM_PUSH_ATTEMPTS", "5"))
    for attempt in range(attempts):
        p = subprocess.run(["git", "-C", str(ROOT), "push", "-u", "origin", branch], capture_output=True, text=True)
        if p.returncode == 0:
            print(f"контрольная точка отправлена в origin/{branch}")
            return
        if attempt < attempts - 1:
            time.sleep(delay)
            delay *= 2
    die(f"push не удался: {p.stderr.strip()}")


def cmd_render(args, state):
    atomic_write(STATE_MD, render(state))


def problems(state: dict, skip_render: bool = False) -> list[str]:
    p: list[str] = []
    try:
        sl = state["slices"]
        order = list(sl)
        if sum(1 for s in sl.values() if s["status"] == "in_progress") > 1:
            p.append("больше одного среза в работе")
        cur = state["current_slice"]
        if cur is not None and cur not in sl:
            p.append(f"current_slice={cur} не существует")
        elif cur is not None and sl[cur]["status"] not in ("in_progress", "todo"):
            p.append(f"current_slice={cur} имеет статус {sl[cur]['status']}")
        elif cur != next_slice(state):
            p.append(f"current_slice={cur} не совпадает с ожидаемым {next_slice(state)}")
        for sid, s in sl.items():
            if s["status"] not in STATUSES:
                p.append(f"{sid}: неизвестный статус {s['status']}")
            for d in s["depends"]:
                if d not in sl:
                    p.append(f"{sid}: зависимость {d} не существует")
                elif order.index(d) > order.index(sid):
                    p.append(f"{sid}: зависимость {d} стоит позже по порядку")
                elif s["status"] == "done" and sl[d]["status"] != "done":
                    p.append(f"{sid}: закрыт, но зависимость {d} не закрыта")
            f = slice_file(sid)
            if not f:
                p.append(f"{sid}: нет файла docs/slices/{sid}-*.md")
                continue
            text = f.read_text(encoding="utf-8")
            for h in REQUIRED_HEADINGS:
                if not re.search(rf"^{re.escape(h)}[ \t]*$", text, re.M):
                    p.append(f"{f.name}: нет раздела «{h}»")
            if s["status"] == "done":
                body = summary_body(text)
                if not body or SUMMARY_MARKER in text:
                    p.append(f"{sid}: закрыт, но «Итог» не заполнен")
                if not s.get("finished"):
                    p.append(f"{sid}: закрыт без даты")
        ids = [g["id"] for g in state["gates"]]
        if len(ids) != len(set(ids)):
            p.append("повторяющиеся ID барьеров")
        aids = [a["id"] for a in state["assumptions"]]
        if len(aids) != len(set(aids)):
            p.append("повторяющиеся ID допущений")
        for g in state["gates"]:
            if g["status"] not in ("open", "resolved"):
                p.append(f"барьер {g['id']}: статус {g['status']}")
            for k in [g["slice"]] + g.get("blocks_start", []) + g.get("blocks_finish", []):
                if k not in sl:
                    p.append(f"барьер {g['id']}: срез {k} не существует")
            if g["status"] == "open":
                for k in g.get("blocks_finish", []):
                    if k in sl and sl[k]["status"] == "done":
                        p.append(f"срез {k} закрыт при открытом барьере {g['id']}")
                for k in g.get("blocks_start", []):
                    if k in sl and sl[k]["status"] in ("in_progress", "done"):
                        p.append(f"срез {k} начат при открытом барьере {g['id']}")
    except (KeyError, TypeError) as e:
        p.append(f"state.json повреждён или неполон: {type(e).__name__} {e}")
        return p
    if not skip_render and STATE_MD.exists() and STATE_MD.read_text(encoding="utf-8") != render(state):
        p.append("docs/STATE.md не соответствует state.json (запустите render)")
    return p


def cmd_check(args, state):
    p = problems(state)
    for x in p:
        print("ОШИБКА:", x)
    if p:
        sys.exit(1)
    print("память проекта согласована")


MUTATING = {"start", "finish", "block", "unblock", "gate-add", "gate-resolve", "assume", "log", "checkpoint", "render"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, has_slice, has_msg in [
        ("brief", cmd_brief, False, False), ("sync", cmd_sync, False, False),
        ("start", cmd_start, True, False), ("finish", cmd_finish, True, True),
        ("block", cmd_block, True, True), ("unblock", cmd_unblock, True, False),
        ("gate-add", cmd_gate_add, True, True), ("gate-resolve", cmd_gate_resolve, True, True),
        ("assume", cmd_assume, True, True), ("log", cmd_log, False, True),
        ("checkpoint", cmd_checkpoint, False, True), ("render", cmd_render, False, False),
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
        if name == "gate-add":
            sp.add_argument("--blocks-start", nargs="+", metavar="SLICE")
            sp.add_argument("--blocks-finish", nargs="+", metavar="SLICE")
        sp.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    with (locked() if args.cmd in MUTATING else contextlib.nullcontext()):
        state = load()
        if hasattr(args, "slice") and args.cmd not in ("gate-resolve", "assume") and args.slice not in state["slices"]:
            die(f"нет среза {args.slice}")
        args.fn(args, state)


if __name__ == "__main__":
    main()
