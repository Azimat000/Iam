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
if args[:2] == ["network", "inspect"]:
    if os.environ.get("SHIM_DEAD"):
        sys.exit(1)
    say("false" if "sandbox_internal" in bad else "true")
if os.environ.get("SHIM_DEAD"):
    print("docker: Cannot connect to the Docker daemon", file=sys.stderr)
    sys.exit(125)
if args[:1] != ["run"] or "iam-claude:1" not in args:
    sys.exit(125)
cmd = args[args.index("iam-claude:1") + 1:]
stdin = sys.stdin.read()
if cmd[:1] == ["claude"]:
    if "stream-json" in cmd:
        if "auth" in bad:
            say(json.dumps({"type": "result", "is_error": True, "api_error_status": 401, "result": "Failed to authenticate"}))
        if "no_init" in bad:
            say(json.dumps({"type": "result", "is_error": False, "result": "ok"}))
        say(json.dumps({"type": "system", "subtype": "init", "tools": ["Bash", "Read"] if "no_tools" in bad else []}) + "\n"
            + json.dumps({"type": "result", "is_error": False, "result": "ok"}))
    if "auth" in bad:
        say(json.dumps({"is_error": True, "api_error_status": 401, "result": "Failed to authenticate", "terminal_reason": "api_error"}))
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
    say("started\n/var/run/docker.sock" if "no_docker_socket" in bad else "started")
if "rootfs-test" in payload:
    # дыра — реалистичная: корень записываемый, но /tmp без исполнения (exec=126)
    say("ro=0\ntmp=0\nexec=126" if "read_only_root" in bad else "ro=1\ntmp=0\nexec=126")
if "CapEff" in payload:
    # дыра — реалистичная: у не-root CapEff=0, но ограничивающий набор не сброшен (--cap-drop ALL не применён)
    say("CapEff:\t0000000000000000\nCapBnd:\t00000000a80425fb\nNoNewPrivs:\t1" if "no_capabilities" in bad
        else "CapEff:\t0000000000000000\nCapBnd:\t0000000000000000\nNoNewPrivs:\t1")
if "pids.max" in payload:
    say("max\nmax" if "limits" in bad else "128\n1073741824")
if "--noproxy" in payload:
    say("200" if "net_direct_denied" in bad else "000")
if "example.com" in payload:
    say("200" if "net_denied" in bad else "000")
if "api.anthropic.com" in payload:
    say("000" if "net_allowed" in bad else "404")
sys.exit(1)
