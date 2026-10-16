#!/usr/bin/env python3
"""Подделка `docker` для проверки сценария verify_isolation.sh без Docker: отвечает так, как отвечал бы «идеальный»
контейнер, а для проверок из SHIM_BAD (через запятую) — как контейнер с дырой."""
import json
import os
import sys

args = sys.argv[1:]
bad = set(filter(None, os.environ.get("SHIM_BAD", "").split(",")))


def say(text=""):
    print(text)
    sys.exit(0)


if args[:1] == ["info"]:
    sys.exit(0)
if args[:1] != ["run"] or "iam-claude:1" not in args:
    sys.exit(125)
cmd = args[args.index("iam-claude:1") + 1:]
stdin = sys.stdin.read()
if cmd[:1] == ["claude"]:
    if "auth" in bad:
        say(json.dumps({"is_error": True, "api_error_status": 401, "result": "Failed to authenticate", "terminal_reason": "api_error"}))
    if "no_tools" in bad and "List the names" in stdin:
        say(json.dumps({"is_error": False, "result": "", "structured_output": {"proposals": [], "explanation": "Bash, Read, Edit"}}))
    if "List the names" in stdin:
        say(json.dumps({"is_error": False, "result": "", "structured_output": {"proposals": [], "explanation": "none"}, "permission_denials": []}))
    if "format" in bad:
        say(json.dumps({"is_error": False, "result": "Конечно! Вот мои мысли."}))
    say(json.dumps({"is_error": False, "result": "", "structured_output": {"proposals": []}, "num_turns": 1}))
if cmd == ["uname", "-m"]:
    say("x86_64" if "arch" in bad else "aarch64")
if cmd == ["id", "-u"]:
    say("0" if "non_root" in bad else "10001")
payload = cmd[2] if cmd[:2] == ["sh", "-c"] else ""
if "/proc/mounts" in payload:
    say("/\n/proc\n/sys\n/dev\n/dev/pts\n/tmp\n/home/claude\n/etc/hosts" + ("\n/Users/me/Documents" if "no_mounts" in bad else ""))
if "control.txt" in payload:
    path = payload.split("'")[1]
    say(open(path).read() if "canary_file" in bad else f"cat: {path}: No such file or directory")
if payload == "env":
    say("PATH=/usr/bin\nHOME=/home/claude" + ("\nTELEGRAM_BOT_TOKEN=123" if "canary_env" in bad else ""))
if "docker.sock" in payload:
    say("/var/run/docker.sock" if "no_docker_socket" in bad else "")
if "rootfs-test" in payload:
    say("rc=0\ntmp_rc=0\nexec_rc=0" if "read_only_root" in bad else "touch: Read-only file system\nrc=1\ntmp_rc=0\nexec_rc=126")
if "CapEff" in payload:
    say("CapEff:\t00000000a80425fb\nNoNewPrivs:\t0" if "no_capabilities" in bad else "CapEff:\t0000000000000000\nNoNewPrivs:\t1")
if "pids.max" in payload:
    say("max\nmax" if "limits" in bad else "128\n1073741824")
if "--noproxy" in payload:
    say("200" if "net_direct_denied" in bad else "000")
if "example.com" in payload:
    say("200" if "net_denied" in bad else "000")
if "api.anthropic.com" in payload:
    say("000" if "net_allowed" in bad else "404")
sys.exit(1)
