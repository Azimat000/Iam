"""Безопасные тела физических ответов до проверки схемы; stdin не сохраняется."""
import hashlib
import json

from iam.backup.scan import SecretScanner
from iam.providers.codex.classify import _json


def record_answer(artifacts, cell, index, run, *, auth=None):
    messages, secrets, malformed = [], [], 0
    credentials = [auth] if auth else []
    for line in run.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = _json(line)
        except (ValueError, RecursionError):
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        if event.get("type") == "iam.auth.updated":
            credentials.append(event.get("auth"))
        item = event.get("item")
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
            messages.append(item["text"])
    for credential in credentials:
        if isinstance(credential, dict) and isinstance(credential.get("tokens"), dict):
            secrets.extend(value for value in credential["tokens"].values()
                           if isinstance(value, str) and len(value) >= 4)
    scanner = SecretScanner(canaries=secrets)
    counts = {}
    messages = artifacts.clean(scanner.redact_value(messages, counts))
    serialized = json.dumps(messages, ensure_ascii=False)
    if scanner.find(serialized) or artifacts.find(serialized):
        raise ValueError("Physical answer failed secret scan")
    record = {"index": index, "returncode": run.returncode, "timed_out": run.timed_out,
              "stderr_present": bool(run.stderr), "messages": messages,
              "malformed_stream_lines": malformed, "redactions": sum(counts.values()),
              "body_available": len(messages) == 1,
              "messages_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
              "scope": "Completed agent message bodies only; credentials, reasoning and diagnostics omitted."}
    artifacts.write(f"{cell}/physical/{index:03d}.json", record, exclusive=True)
    return record
