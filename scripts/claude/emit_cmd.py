#!/usr/bin/env python3
"""Печатает команду `docker run` (те же флаги изоляции, что у личности) с произвольной командой внутри.

  python3 scripts/claude/emit_cmd.py [--token-file ФАЙЛ] -- sh -c 'id -u'

Нужна `verify_isolation.sh`, чтобы проверять именно ту оболочку, в которой работает исполнитель, а не её копию.
"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from iam.providers import SandboxConfig, docker_run   # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--image", default="iam-claude:1")
    ap.add_argument("--network", default="iam-sandbox")
    ap.add_argument("--claude", action="store_true", help="не подменять команду: печатать рабочий запуск claude -p")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command and not args.claude:
        ap.error("нужна команда после -- (или флаг --claude)")
    cfg = SandboxConfig(image=args.image, network=args.network, token_file=args.token_file)
    cmd = docker_run(cfg, model=args.model) if args.claude else docker_run(cfg, command=command)
    print(shlex.join(cmd.argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
