"""Inspect a blocked Codex event without storing raw stream data or applying a response."""

import argparse
import json
from pathlib import Path

from iam.domain import jsonschema
from iam.intentions.completion import valid_response
from iam.providers import CodexProvider, CodexSandboxConfig, subprocess_runner
from iam.providers.codex.classify import _json, tool_use_categories

from integration.environment_pair import IMAGE
from integration.goal_series import artifacts
from integration.live_series import MODEL, ROOT, StopSeries, sha


REQUEST_TRACE_SHA = '792367f10faa4da3f48b4ea6c5e0bf0defec27fd9b2a00d7f815d90dc4acb526'
EVENT_PHASES = {'item.started': 'started', 'item.updated': 'updated', 'item.completed': 'completed'}
# Closed enum in codex-rs/exec/src/exec_events.rs at rust-v0.166.0. Names are for the report only.
PUBLIC_ITEM_TYPES = frozenset(('agent_message', 'reasoning', 'command_execution', 'file_change',
                               'mcp_tool_call', 'web_search', 'todo_list', 'collab_tool_call', 'error'))


def item_type(item):
    """Return a fixed encoding and an exact name only from a public allowlist."""
    if 'type' not in item:
        return 'missing', ''
    value = item['type']
    if value is None:
        return 'null', ''
    if not isinstance(value, str):
        return 'non_string', ''
    return 'string', value if value in PUBLIC_ITEM_TYPES else ''


def error_message_shape(item):
    message = item.get('message')
    if not isinstance(message, str):
        return 'missing_or_non_string'
    # Exec 0.166.0 serializes ModelRerouted as a message prefix, without a separate field.
    return 'model_reroute_prefix' if message.startswith('model rerouted: ') else 'other_message'


def inspect_stream(run):
    findings = set()
    for line in run.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = _json(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        phase = EVENT_PHASES.get(event.get('type'), 'other') if isinstance(event.get('type'), str) else 'other'
        for category in tool_use_categories(event):
            encoding, name = ('not_applicable', '') if category == 'event_type_pattern' else item_type(event['item'])
            message_shape = error_message_shape(event['item']) if name == 'error' else ''
            findings.add((category, phase, encoding, name, message_shape))
    return {'returncode': run.returncode, 'timed_out': run.timed_out, 'stderr_present': bool(run.stderr),
            'findings': [dict(category=category, phase=phase, encoding=encoding,
                              public_type=name or None, message_shape=message_shape or None)
                         for category, phase, encoding, name, message_shape in sorted(findings)]}


def inspect_once(source, output, auth, preflight):
    source, output = Path(source).resolve(), Path(output).resolve()
    auth = Path(auth).expanduser().absolute()
    if sha(source) != REQUEST_TRACE_SHA:
        raise ValueError('unexpected_request_trace')
    request = json.loads(source.read_text())['request']
    if jsonschema.errors('request', request) or request['purpose'] != 'appraise' or request['allowed_proposals'] != ['intention']:
        raise ValueError('unexpected_request')
    report = json.loads(Path(preflight).read_text())
    if (report['image'] != IMAGE or report['model_calls'] != 0
            or not all(report[key] is True for key in ('isolation_passed', 'schema_matches', 'launcher_matches'))):
        raise ValueError('preflight_failed')
    if output.is_relative_to(ROOT) or auth.resolve().is_relative_to(output) or source.is_relative_to(output):
        raise ValueError('external_evidence_required')
    config = CodexSandboxConfig(auth_file=str(auth), image=IMAGE, persist_auth_updates=True)
    output.mkdir(mode=0o700)
    audit = artifacts(output, auth)
    audit.write('manifest.json', dict(source_sha256=REQUEST_TRACE_SHA, image=IMAGE, model=MODEL,
                max_physical_calls=1, timeout_seconds=60, retries=0, applies_response=False), exclusive=True)
    attempts = 0

    def transport(argv, stdin, timeout, cleanup=None):
        nonlocal attempts
        if attempts:
            raise StopSeries('diagnostic_call_cap')
        audit.write('attempt.json', {'index': 1}, exclusive=True)
        attempts += 1
        run = subprocess_runner(argv, stdin, min(timeout, 60), cleanup)
        audit.write('stream.json', inspect_stream(run), exclusive=True)
        return run

    provider = CodexProvider(transport, config, model=MODEL, timeout_s=60, max_format_retries=0)
    try:
        response = provider.propose(request)
        status = 'valid' if valid_response(response, request) else 'blocked'
        summary = dict(status=status, tool_use_detected=response['tool_use_detected'])
    except Exception as error:
        summary = dict(status='error', error_class=type(error).__name__)
    summary.update(physical_calls=attempts, applied=False)
    audit.write('summary.json', summary, exclusive=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('source', 'output', 'auth-file', 'preflight'):
        parser.add_argument('--' + argument, type=Path, required=True)
    args = parser.parse_args()
    summary = inspect_once(args.source, args.output, args.auth_file, args.preflight)
    print(json.dumps(summary))
    return int(summary['status'] == 'error')


if __name__ == '__main__':
    raise SystemExit(main())
