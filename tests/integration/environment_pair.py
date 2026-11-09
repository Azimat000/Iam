"""D-091: один парный опыт из общего прошлого E, с отдельным процессом на сеанс."""

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from iam.intentions.completion import valid_response
from iam.providers import CodexProvider, CodexSandboxConfig, subprocess_runner
from iam.providers.codex.command import RESPONSE_JSON_SCHEMA
from iam.state import StateController, verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, rest
from iam.world import PersistentWorld, READ, fingerprint

from integration.answer_audit import record_answer
from integration.goal_series import artifacts, load
from integration.intention_probe import OptionsExperimentCycle, ProbeProvider, frozen_sources, snapshot
from integration.live_series import MODEL, PROFILE_PATH, ROOT, StopSeries, audit, sha
from integration.test_persistent_core import tick, wire

VERSION = 's25-environment-pair-1'
DESIGN = ROOT / 'docs/evidence/S25-environment-pair-design.md'
IMAGE = 'sha256:0af0b2e38184cbcca28395976e59e8579d24bb06b63980277278e99ce271e43f'
SOURCE_SHA = '5b4807521406c0a3821023569f846f9a0754690025d810d2d4a21c78d394ef71'
APPROVED_CORE = '51839da3eaff17e157799f922bf25df42c27d201'
BRANCHES = ('empty', 'library')
SCHEDULE = tuple(f'{branch}-{number}' for number in range(1, 5) for branch in BRANCHES)
CALLS_PER_SESSION = 6
MAX_CALLS = len(SCHEDULE) * CALLS_PER_SESSION
SESSION_MS = 1_800_000
LOAD_REASON = 'EXPERIMENTAL: D-090, материалы для парной проверки среды'


def sources():
    return {**frozen_sources(), str(DESIGN.relative_to(ROOT)): sha(DESIGN),
            'docker/launch.cjs': sha(ROOT / 'docker/launch.cjs'),
            'docker/response.schema.json': sha(ROOT / 'docker/response.schema.json')}


def contents():
    materials = re.findall(r'^\d\. \*\*(.*?)\*\*\n\n   ```text\n   ([^\n]+)\n   ```',
                           DESIGN.read_text(), flags=re.MULTILINE)
    if len(materials) != 2 or len({title for title, _ in materials}) != 2:
        raise ValueError('expected_two_fixed_materials')
    return materials


def events(store):
    return [event.to_dict() for event in store.events()]


def guard_store(store):
    if verify(store) or store.integrity_problems():
        raise StopSeries('state_integrity')


def prepare(source, output):
    common = output / 'common/E.db'
    snapshot(source, common)
    with Store.open(common, readonly=True) as store:
        guard_store(store)
        prefix = events(store)
        baseline = store.latest_snapshot().state
        if len(prefix) != 104 or baseline['intentions'] or baseline['world_time']['world'].get('library'):
            raise StopSeries('source_history_mismatch')
    materials = contents()
    branches = {}
    for branch in BRANCHES:
        path = output / f'stories/{branch}/persona.db'
        snapshot(common, path)
        with Store.open(path) as store:
            if events(store) != prefix or store.latest_snapshot().state != baseline:
                raise StopSeries('different_initial_history')
            keys = []
            if branch == 'library':
                ctl = StateController(store)
                world = PersistentWorld(ctl)
                keys = [world.add_material(title, body, LOAD_REASON) for title, body in materials]
                expected = deepcopy(baseline)
                expected['world_time']['world']['library'] = {
                    key: dict(title=title, text=body, origin='operator_provided', fingerprint=fingerprint(body), read=False)
                    for key, (title, body) in zip(keys, materials, strict=True)}
                if ctl.state != expected:
                    raise StopSeries('different_initial_state')
            guard_store(store)
            journal = events(store)
            if journal[:len(prefix)] != prefix or [event['type'] for event in journal[len(prefix):]] != [
                    'operator.intervention'] * len(keys):
                raise StopSeries('different_initial_interventions')
            branches[branch] = {'head': journal[-1], 'material_ids': keys}
    return branches


class PairProvider(ProbeProvider):
    def propose(self, request):
        world = self.active['world'].state
        context = json.dumps(request['context'], ensure_ascii=False)
        for key, material in world.get('library', {}).items():
            if key in self.active['materials'] and not material['read'] and (
                    material['text'] in context or material['fingerprint'] in context):
                raise StopSeries('unread_material_exposed')
        response = super().propose(request)
        if not valid_response(response, request):
            raise StopSeries('tool_or_inconsistent_response')
        return response


def check_frozen(root, manifest, summary):
    if (sha(root / 'manifest.json') != summary['manifest_sha256'] or sources() != manifest['sources']
            or sha(root / 'common/E.db') != manifest['common_sha256']
            or not source_matches(Path(manifest['source']))):
        raise StopSeries('frozen_sources_changed')


def source_matches(source):
    wal = source.with_suffix('.db-wal')
    return sha(source) == SOURCE_SHA and (not wal.exists() or wal.stat().st_size == 0)


def call_transport(root, manifest, summary, session_name, session, output):
    def transport(argv, stdin, timeout, cleanup=None):
        check_frozen(root, manifest, summary)
        attempts = summary['attempts']
        branch = session_name.split('-')[0]
        if (len(attempts) >= MAX_CALLS
                or sum(row['session'].startswith(branch + '-') for row in attempts) >= MAX_CALLS // 2
                or sum(row['session'] == session_name for row in attempts) >= CALLS_PER_SESSION):
            raise StopSeries('physical_call_cap')
        session.charge()
        remaining = (SESSION_MS - session.usage()['elapsed_ms']) / 1000
        if remaining <= 0:
            raise StopSeries('session_deadline')
        attempt = {'index': len(attempts) + 1, 'session': session_name, 'status': 'reserved'}
        attempts.append(attempt)
        output.write('summary.json', summary)
        try:
            result = subprocess_runner(argv, stdin, min(timeout, 60, remaining), cleanup)
            body = record_answer(output, 'sessions/' + session_name, attempt['index'], result,
                                 auth=json.loads(stdin)['auth'])
        except Exception as error:
            attempt.update(status='error', error_class=type(error).__name__)
            output.write('summary.json', summary)
            raise
        attempt.update(status='returned', returncode=result.returncode, timed_out=result.timed_out,
                       tool_use_categories=body['tool_use_categories'])
        output.write('summary.json', summary)
        if body['redactions']:
            raise StopSeries('secret_in_physical_answer')
        return result
    return transport


def stub_provider(material_ids):
    def reply(request):
        if request['purpose'] != 'choose_action':
            return Reply(())
        if material_ids:
            return Reply((act(READ, {'material_id': material_ids[0]}),))
        return Reply((rest(),))
    return ScriptedProvider([reply] * CALLS_PER_SESSION)


def run_session(root, name, auth):
    manifest, summary = load(root, 'manifest.json'), load(root, 'summary.json')
    output = artifacts(root, auth)
    check_frozen(root, manifest, summary)
    count = len(summary['sessions'])
    if (summary['status'] != 'running' or count >= len(SCHEDULE) or SCHEDULE[count] != name
            or any(record['status'] != 'completed' for record in summary['sessions'])):
        raise StopSeries('session_order_or_replay')
    branch, number = name.split('-')
    info = manifest['branches'][branch]
    head = info['head'] if number == '1' else summary['sessions'][count - 2]['head']
    record = {'name': name, 'process_id': os.getpid(), 'status': 'started'}
    output.write(f'sessions/{name}/started.json', record, exclusive=True)
    summary['sessions'].append(record)
    output.write('summary.json', summary)
    try:
        with Store.open(root / f'stories/{branch}/persona.db') as store:
            guard_store(store)
            if events(store)[-1] != head:
                raise StopSeries('branch_changed_between_sessions')
            ctl = StateController(store)
            active = {'materials': info['material_ids']}
            live = manifest['mode'] == 'live'
            if live:
                def transport(*args):
                    return active['transport'](*args)
                inner = CodexProvider(transport, CodexSandboxConfig(auth_file=str(auth), image=IMAGE,
                                      persist_auth_updates=True), model=MODEL, timeout_s=60, max_format_retries=0)
            else:
                ids = [key for key in info['material_ids'] if not ctl.state['world_time']['world']['library'][key]['read']]
                inner = stub_provider(ids)
            provider = audit.AuditedProvider(PairProvider(inner, active), output, 'sessions/' + name, disclose=False)
            session, cycle, _ = wire(ctl, provider=provider, charge=not live,
                                     cycle_type=OptionsExperimentCycle, max_rerequest=0)
            active.update(memory=cycle.memory, world=cycle.world,
                          transport=call_transport(root, manifest, summary, name, session, output))
            if ctl.session_state == 'paused' and number == '1':
                session.command('stop', 'EXPERIMENTAL: закрыть прошлый сеанс')
            if ctl.session_state != 'stopped':
                raise StopSeries('unexpected_session_state')
            record['recovered_goals'] = ctl.state['intentions']
            session.command('start', 'EXPERIMENTAL: обычный сеанс', max_calls=CALLS_PER_SESSION, max_ms=SESSION_MS)
            check_frozen(root, manifest, summary)
            if MAX_CALLS - len(summary['attempts']) < CALLS_PER_SESSION:
                raise StopSeries('global_forecast')
            before = store.last_seq()
            summary['decisions'] += 1
            output.write('summary.json', summary)
            report = asdict(tick(cycle))
            new_events = [event.to_dict() for event in ctl.events_after(before)]
            faults = audit.decision_technical_faults(report, new_events, provider.traces)[0]
            faults = [fault for fault in faults if not (fault['source'] == 'event'
                      and fault['event']['payload'].get('fault') == 'creator_undelivered')]
            faults += [event for event in new_events if event['type'] == 'proposal.stale']
            assessment_errors = audit.assessment_errors(new_events)
            usage = session.usage()
            output.write(f'sessions/{name}/decision.json', dict(report=report, events=new_events,
                         state=ctl.state, usage=usage, faults=faults, assessment_errors=assessment_errors,
                         provider_failure=provider.failure), exclusive=True)
            guard_store(store)
            if (faults or assessment_errors or provider.failure or ctl.session_state != 'running'
                    or usage['elapsed_ms'] >= SESSION_MS or report['outcome'] in ('failed', 'no_decision', 'skipped')
                    or not any(trace['request']['purpose'] == 'choose_action' and trace['error'] is None
                               and valid_response(trace['response'], trace['request']) for trace in provider.traces)):
                raise StopSeries('technical_cycle_failure')
            session.command('stop', 'EXPERIMENTAL: конец сеанса')
            guard_store(store)
            record.update(status='completed', head=events(store)[-1], outcome=report['outcome'],
                          typed_requests=len(provider.traces), session_calls=usage['calls'])
            output.write(f'sessions/{name}/final.json', dict(record=record, state=ctl.state,
                         memories=[row.to_dict() for row in cycle.memory.records()]), exclusive=True)
        check_frozen(root, manifest, summary)
    except Exception as error:
        record['status'] = 'stopped'
        summary.update(status='stopped', stop={'session': name, 'error_class': type(error).__name__,
                       'reason': str(error) if isinstance(error, StopSeries) else 'worker_exception'})
    output.write('summary.json', summary)
    return int(summary['status'] != 'running')


def run_pair(source, root, *, auth=None, preflight=None):
    source, root = Path(source).resolve(), Path(root).resolve()
    auth = Path(auth).expanduser().absolute() if auth else None
    if not source_matches(source):
        raise ValueError('wrong_source_snapshot')
    if root.is_relative_to(ROOT) or (auth and auth.resolve().is_relative_to(root)):
        raise ValueError('external_evidence_required')
    environment = None
    if auth:
        environment = json.loads(Path(preflight).read_text())
        if (environment['image'] != IMAGE or environment['model_calls'] != 0
                or not all(environment[key] is True for key in ('isolation_passed', 'schema_matches', 'launcher_matches'))):
            raise ValueError('preflight_failed')
        subprocess.run(['git', 'diff', '--quiet', APPROVED_CORE, '--', 'src', 'docker', 'scenarios/personas'],
                       cwd=ROOT, check=True)
    root.mkdir(mode=0o700)
    output = artifacts(root, auth)
    branches = prepare(source, root)
    manifest = dict(version=VERSION, mode='live' if auth else 'stub', model=MODEL, image=IMAGE,
                    sources=sources(), core=APPROVED_CORE, source=str(source), source_sha256=SOURCE_SHA,
                    common_sha256=sha(root / 'common/E.db'), branches=branches, preflight=environment,
                    profile_sha256=sha(PROFILE_PATH), schema=RESPONSE_JSON_SCHEMA, schedule=SCHEDULE,
                    max_decisions=len(SCHEDULE), max_model_calls=MAX_CALLS, max_calls_per_session=CALLS_PER_SESSION,
                    session_ms=SESSION_MS, native_retries=0, cycle_retries=0, injected_inputs=0)
    output.write('manifest.json', manifest, exclusive=True)
    summary = dict(status='running', manifest_sha256=sha(root / 'manifest.json'), sessions=[], decisions=0, attempts=[])
    output.write('summary.json', summary, exclusive=True)
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(str(ROOT / path) for path in ('src', 'tests')),
           'PYTHONDONTWRITEBYTECODE': '1'}
    for name in SCHEDULE:
        args = [sys.executable, '-m', 'integration.environment_pair', '--output', str(root), '--worker', name]
        if auth:
            args += ['--auth-file', str(auth)]
        worker = subprocess.run(args, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        summary = load(root, 'summary.json')
        if worker.returncode or summary['status'] != 'running':
            if summary['status'] == 'running':
                summary.update(status='stopped', stop={'session': name, 'reason': 'worker_failed'})
            break
        try:
            check_frozen(root, manifest, summary)
        except StopSeries as error:
            summary.update(status='stopped', stop={'session': name, 'reason': str(error)})
            break
    else:
        summary['status'] = 'completed'
    output.write('summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--auth-file', type=Path)
    parser.add_argument('--preflight', type=Path)
    parser.add_argument('--worker', choices=SCHEDULE)
    args = parser.parse_args()
    if args.worker:
        return run_session(args.output, args.worker, args.auth_file)
    if not args.source or (args.auth_file and not args.preflight):
        parser.error('source is required, live also requires preflight')
    result = run_pair(args.source, args.output, auth=args.auth_file, preflight=args.preflight)
    print(json.dumps({key: result[key] for key in ('status', 'decisions')} |
                     {'physical_calls': len(result['attempts']), 'stop': result.get('stop')}, ensure_ascii=False))
    return int(result['status'] != 'completed')


if __name__ == '__main__':
    raise SystemExit(main())
