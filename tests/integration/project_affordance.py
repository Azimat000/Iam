"""D-093. Run the fixed project guidance comparison in separate experimental sessions."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from iam.intentions.completion import valid_response
from iam.providers import CodexProvider, CodexSandboxConfig, subprocess_runner
from iam.state import StateController
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act, rest
from iam.world import READ
from iam.world.cycle import GUIDANCE, PROJECT_CREATION

from integration.answer_audit import record_answer
from integration.codex_item_diagnostic import inspect_stream
from integration.environment_pair import IMAGE, PairProvider, events, guard_store
from integration.goal_series import artifacts, load
from integration.intention_probe import OptionsExperimentCycle, frozen_sources, snapshot
from integration.live_series import MODEL, ROOT, ExperimentCycle, StopSeries, audit, initial_database, sha
from integration.test_persistent_core import tick, wire

DESIGN = ROOT / 'docs/evidence/S25-project-affordance-design.md'
BRANCHES = ('e-library', 'independent', 'e-legacy', 'e-empty')
SESSIONS, DECISIONS, PER_DECISION = 3, 2, 4
SESSION_MS = 1_800_000
SOURCE_FILES = {
    'e-empty': ('common/E.db', '5b4807521406c0a3821023569f846f9a0754690025d810d2d4a21c78d394ef71'),
    'e-library': ('stories/library/persona.db', 'dd19e3e1449ab4586c3335755b0514a44ff9d904c16e11f6f4d258730e244ce3'),
}
FOLLOWUP_DESIGN = ROOT / 'docs/evidence/S25-after-reading-design.md'
FOLLOWUP_CYCLES = {'e-options': 'options', 'independent-options': 'options',
                   'e-direct': 'direct', 'independent-direct': 'direct'}
FOLLOWUP_SNAPSHOTS = {
    'e': ('stories/e-library/persona.db', '5ca177ab73ca9264d7896be20ed0ce87f995dc81a46d240eb5568d510fc65eec', 186),
    'independent': ('stories/independent/persona.db', '26ba03757f2ae97d2bd6cb1a8ba6b3d924452b9fde2381dc374441da2448d9c4', 82),
}
FOLLOWUP_HASHES = {
    'manifest.json': '487f53439f6e67745602488aa13d7c3b66c6d1929a1b034042ea0ed8a8fea8e5',
    'summary.json': '04a45c71e38d898b1e411f3bde5f4a49a1e410d3bd76650b38d4683337e98f6d',
    **{path: digest for path, digest, _ in FOLLOWUP_SNAPSHOTS.values()},
}
SERIES_WIDE_FAILURES = {'ProviderAuthError', 'ProviderLimitError', 'ProviderTimeout', 'ProviderUnavailable',
                        'SecretInResponse', 'SessionLimitReached'}


class LegacyGuidanceCycle(ExperimentCycle):
    def _context(self, *args):
        context = super()._context(*args)
        for item in context:
            if item['text'] == GUIDANCE:
                item['text'] = GUIDANCE.replace(PROJECT_CREATION, '', 1)
        return context


class DecisionStopped(Exception):
    """A rejected individual response ends its branch."""


def sources(design=DESIGN):
    paths = (design, ROOT / 'docker/launch.cjs', ROOT / 'docker/response.schema.json')
    return {**frozen_sources(), **{str(path.relative_to(ROOT)): sha(path) for path in paths}}


def prepare(root, source):
    baseline = {}
    for branch, (relative, digest) in SOURCE_FILES.items():
        path = source / relative
        wal = path.with_suffix('.db-wal')
        if sha(path) != digest or (wal.exists() and wal.stat().st_size):
            raise StopSeries('unexpected_source_snapshot')
        snapshot(path, root / f'initial/{branch}.db')
    snapshot(root / 'initial/e-library.db', root / 'initial/e-legacy.db')
    independent = root / 'initial/independent.db'
    initial_database(independent, 'independent')
    materials = re.findall(r'^\*\*(.+)\*\*\n\n```text\n([^\n]+)\n```', DESIGN.read_text(), re.MULTILINE)
    if len(materials) != 2:
        raise StopSeries('unexpected_material_count')
    with Store.open(independent) as store:
        _, cycle, _ = wire(StateController(store))
        for title, text in materials:
            cycle.world.add_material(title, text, 'EXPERIMENTAL: материалы независимой истории D-093')
    histories, states = {}, {}
    for branch in BRANCHES:
        initial = root / f'initial/{branch}.db'
        with Store.open(initial, readonly=True) as store:
            guard_store(store)
            histories[branch] = events(store)
            states[branch] = store.latest_snapshot().state
            library = states[branch]['world_time']['world'].get('library', {})
            if any(material['read'] for material in library.values()):
                raise StopSeries('initial_material_read')
            baseline[branch] = dict(sha256=sha(initial), head=histories[branch][-1], materials=list(library))
        snapshot(initial, root / f'stories/{branch}/persona.db')
    common, library = histories['e-empty'], histories['e-library']
    if (len(common) != 104 or library[:104] != common or library != histories['e-legacy']
            or [event['type'] for event in library[104:]] != ['operator.intervention'] * 2):
        raise StopSeries('different_initial_history')
    if states['e-library'] != states['e-legacy']:
        raise StopSeries('different_library_state')
    empty_world = states['e-empty']['world_time']['world']
    library_world = states['e-library']['world_time']['world']
    if len(library_world.get('library', {})) != 2 or empty_world.get('library'):
        raise StopSeries('unexpected_library_contents')
    library_world.pop('library')
    empty_world.pop('library', None)
    if states['e-empty'] != states['e-library']:
        raise StopSeries('different_state_outside_library')
    return baseline


def prepare_followup(root, source):
    if any(sha(source / path) != digest for path, digest in FOLLOWUP_HASHES.items()):
        raise StopSeries('unexpected_d093_source')
    baseline = {}
    for story, (relative, _, length) in FOLLOWUP_SNAPSHOTS.items():
        path = source / relative
        wal = path.with_suffix('.db-wal')
        if wal.exists() and wal.stat().st_size:
            raise StopSeries('unexpected_source_wal')
        with Store.open(path, readonly=True) as store:
            guard_store(store)
            journal, state = events(store), store.latest_snapshot().state
        library = state['world_time']['world']['library']
        if (len(journal) != length or state['intentions'] or len(library) != 2
                or not all(material['read'] for material in library.values())):
            raise StopSeries('unexpected_reading_history')
        for kind in ('options', 'direct'):
            branch = f'{story}-{kind}'
            initial = root / f'initial/{branch}.db'
            snapshot(path, initial)
            with Store.open(initial, readonly=True) as store:
                if events(store) != journal or store.latest_snapshot().state != state:
                    raise StopSeries('different_initial_state')
            baseline[branch] = dict(sha256=sha(initial), head=journal[-1], materials=list(library))
            snapshot(initial, root / f'stories/{branch}/persona.db')
    return baseline


def check_frozen(root, manifest, summary):
    if (sha(root / 'manifest.json') != summary['manifest_sha256']
            or sources(ROOT / manifest['design']) != manifest['sources']
            or any(sha(root / f'initial/{name}.db') != item['sha256']
                   for name, item in manifest['initial'].items())
            or any(sha(Path(manifest['source']) / relative) != digest
                   for relative, digest in manifest['source_hashes'].items())):
        raise StopSeries('frozen_inputs_changed')


def run_session(root, branch, number, auth):
    manifest, summary = load(root, 'manifest.json'), load(root, 'summary.json')
    output = artifacts(root, auth)
    check_frozen(root, manifest, summary)
    if (summary['status'] != 'running' or (manifest['mode'] == 'live') != bool(auth)
            or (branch, number) != next_session(summary['sessions'], manifest['branches'])):
        raise StopSeries('unexpected_worker')
    decisions = manifest['decisions_per_session']
    per_decision = manifest['calls_per_decision']
    session_calls = decisions * per_decision
    name = f'{branch}-{number}'
    record = dict(name=name, branch=branch, number=number, pid=os.getpid(), status='running')
    output.write(f'sessions/{name}/started.json', record, exclusive=True)
    summary['sessions'].append(record)

    def save():
        output.write('summary.json', summary)

    save()
    path = root / f'stories/{branch}/persona.db'
    try:
        with Store.open(path) as store:
            guard_store(store)
            initial = manifest['initial'][branch]
            previous = [row for row in summary['sessions'][:-1] if row['branch'] == branch]
            if len(previous) != number - 1 or any(row['status'] != 'completed' for row in previous):
                raise StopSeries('session_replay_or_out_of_order')
            expected_head = previous[-1]['head'] if previous else initial['head']
            if events(store)[-1] != expected_head:
                raise StopSeries('story_changed_between_processes')
            ctl = StateController(store)
            active = {'materials': initial['materials']}
            decision_calls = 0

            def transport(argv, stdin, timeout, cleanup=None):
                nonlocal decision_calls
                check_frozen(root, manifest, summary)
                if (decision_calls >= per_decision or len(summary['attempts']) >= manifest['max_calls']
                        or session.usage()['calls'] >= session_calls):
                    raise StopSeries('physical_call_cap')
                session.charge()
                left = (SESSION_MS - session.usage()['elapsed_ms']) / 1000
                if left <= 0:
                    raise StopSeries('session_deadline')
                decision_calls += 1
                attempt = dict(index=len(summary['attempts']) + 1, session=name,
                               decision=summary['decisions'], status='reserved')
                summary['attempts'].append(attempt)
                save()
                try:
                    result = subprocess_runner(argv, stdin, min(timeout, 60, left), cleanup)
                    body = record_answer(output, f'sessions/{name}', attempt['index'], result,
                                         auth=json.loads(stdin)['auth'])
                    attempt.update(status='returned', stream=inspect_stream(result))
                    save()
                    if body['redactions']:
                        raise StopSeries('secret_in_physical_answer')
                except Exception as error:
                    attempt.update(status='error', error_class=type(error).__name__)
                    save()
                    raise
                return result

            if auth:
                inner = CodexProvider(transport, CodexSandboxConfig(auth_file=str(auth), image=IMAGE,
                                      persist_auth_updates=True), model=MODEL, timeout_s=60, max_format_retries=0)
            else:
                def stub(request):
                    if request['purpose'] != 'choose_action':
                        return Reply(())
                    library = cycle.world.state.get('library', {})
                    key = next((key for key, material in library.items() if not material['read']), None)
                    return Reply((act(READ, {'material_id': key}) if key else rest(),))
                inner = ScriptedProvider([stub] * session_calls)
            provider = audit.AuditedProvider(PairProvider(inner, active), output, f'sessions/{name}', disclose=False)
            cycle_type = {'legacy': LegacyGuidanceCycle, 'direct': ExperimentCycle,
                          'options': OptionsExperimentCycle}[manifest['cycles'][branch]]
            session, cycle, _ = wire(ctl, provider=provider, charge=not auth, cycle_type=cycle_type, max_rerequest=0)
            active.update(memory=cycle.memory, world=cycle.world)
            if ctl.session_state == 'paused' and number == 1:
                session.command('stop', 'EXPERIMENTAL: закрыть прошлый сеанс')
            if ctl.session_state != 'stopped':
                raise StopSeries('unexpected_session_state')
            record['recovered_goals'] = cycle.intentions.visible()
            session.command('start', 'EXPERIMENTAL: обычный сеанс',
                            max_calls=session_calls, max_ms=SESSION_MS)
            for index in range(decisions):
                check_frozen(root, manifest, summary)
                if (summary['decisions'] >= manifest['max_decisions']
                        or manifest['max_calls'] - len(summary['attempts']) < per_decision
                        or session.usage()['max_calls'] - session.usage()['calls'] < per_decision):
                    raise StopSeries('decision_forecast')
                summary['decisions'] += 1
                before, trace_before, decision_calls = store.last_seq(), len(provider.traces), 0
                save()
                report = asdict(tick(cycle))
                new_events = [event.to_dict() for event in ctl.events_after(before)]
                traces = provider.traces[trace_before:]
                faults = audit.decision_technical_faults(report, new_events, traces)[0]
                faults = [fault for fault in faults if not (fault['source'] == 'event'
                          and fault['event']['payload'].get('fault') == 'creator_undelivered')]
                faults += [event for event in new_events if event['type'] == 'proposal.stale']
                assessment_errors = audit.assessment_errors(new_events)
                output.write(f'sessions/{name}/decisions/{index + 1:02d}.json', dict(
                    report=report, events=new_events, state=ctl.state, usage=session.usage(), faults=faults,
                    assessment_errors=assessment_errors, provider_failure=provider.failure), exclusive=True)
                guard_store(store)
                failure = provider.failure or {}
                attempts = summary['attempts'][-decision_calls:] if decision_calls else []
                if (failure.get('error_class') in SERIES_WIDE_FAILURES
                        or (failure.get('error_class') == 'StopSeries'
                            and failure.get('diagnostic') != 'tool_or_inconsistent_response')
                        or any(row['status'] == 'error' or row.get('stream', {}).get('timed_out') for row in attempts)):
                    raise StopSeries('transport_or_recording_failure')
                if (failure or faults or assessment_errors or ctl.session_state != 'running'
                        or report['outcome'] in ('failed', 'no_decision', 'skipped')
                        or not any(row['request']['purpose'] == 'choose_action'
                                   and valid_response(row['response'], row['request']) for row in traces)):
                    raise DecisionStopped('technical_cycle_failure')
            session.command('stop', 'EXPERIMENTAL: конец сеанса')
            guard_store(store)
            record.update(status='completed', head=events(store)[-1], usage=session.usage())
            output.write(f'sessions/{name}/final.json', dict(
                record=record, state=ctl.state, memories=[row.to_dict() for row in cycle.memory.records()]), exclusive=True)
        check_frozen(root, manifest, summary)
    except DecisionStopped:
        record.update(status='branch_stopped', reason='technical_cycle_failure')
    except Exception as error:
        record.update(status='series_stopped', reason=type(error).__name__)
        if isinstance(error, StopSeries):
            record['code'] = str(error)
    save()
    return 0 if record['status'] == 'completed' else 1


def next_session(sessions, branches=BRANCHES):
    if not sessions:
        return branches[0], 1
    last = sessions[-1]
    if last['status'] == 'completed' and last['number'] < SESSIONS:
        return last['branch'], last['number'] + 1
    if last['status'] in ('completed', 'branch_stopped'):
        index = branches.index(last['branch']) + 1
        if index < len(branches):
            return branches[index], 1
    return None


def run_series(root, source, auth=None, preflight=None, *, followup=False):
    root, source = Path(root).resolve(), Path(source).resolve()
    auth = Path(auth).expanduser().absolute() if auth else None
    if root.is_relative_to(ROOT) or root.is_relative_to(source) or (auth and auth.resolve().is_relative_to(root)):
        raise ValueError('external_evidence_required')
    environment = load(Path(preflight).parent, Path(preflight).name) if auth else None
    if auth and (environment['image'] != IMAGE or environment['model_calls'] != 0
                 or not all(environment[key] is True for key in ('isolation_passed', 'schema_matches', 'launcher_matches'))):
        raise ValueError('preflight_failed')
    root.mkdir(mode=0o700)
    output = artifacts(root, auth)
    if followup:
        initial = prepare_followup(root, source)
        cycles = FOLLOWUP_CYCLES
        design = FOLLOWUP_DESIGN
        decisions, per_decision = 1, 6
        source_hashes = FOLLOWUP_HASHES
    else:
        initial = prepare(root, source)
        cycles = {name: 'legacy' if name in ('e-legacy', 'e-empty') else 'direct' for name in BRANCHES}
        design = DESIGN
        decisions, per_decision = DECISIONS, PER_DECISION
        source_hashes = dict(SOURCE_FILES.values())
    max_decisions = len(cycles) * SESSIONS * decisions
    manifest = dict(version='D-094' if followup else 'D-093', source=str(source), sources=sources(design),
                    design=str(design.relative_to(ROOT)), source_hashes=source_hashes,
                    initial=initial, model=MODEL, image=IMAGE, mode='live' if auth else 'stub', preflight=environment,
                    branches=list(cycles), cycles=cycles, sessions=SESSIONS, decisions_per_session=decisions,
                    max_decisions=max_decisions, max_calls=max_decisions * per_decision,
                    calls_per_decision=per_decision, native_retries=0, cycle_retries=0)
    output.write('manifest.json', manifest, exclusive=True)
    summary = dict(status='running', manifest_sha256=sha(root / 'manifest.json'), decisions=0, attempts=[], sessions=[])
    output.write('summary.json', summary, exclusive=True)
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(str(ROOT / part) for part in ('src', 'tests')),
           'PYTHONDONTWRITEBYTECODE': '1'}
    while scheduled := next_session(summary['sessions'], manifest['branches']):
        branch, number = scheduled
        args = [sys.executable, '-m', 'integration.project_affordance', '--output', str(root),
                '--worker', branch, str(number)]
        if auth:
            args += ['--auth-file', str(auth)]
        worker = subprocess.run(args, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        summary = load(root, 'summary.json')
        rows = summary['sessions']
        if (not rows or rows[-1]['name'] != f'{branch}-{number}'
                or rows[-1]['status'] not in ('branch_stopped', 'completed')
                or (worker.returncode != 0) != (rows[-1]['status'] == 'branch_stopped')):
            summary['status'] = 'stopped'
            break
    if summary['status'] == 'running':
        summary['status'] = ('completed' if summary['decisions'] == max_decisions
                             and all(row['status'] == 'completed' for row in summary['sessions']) else 'partial')
    check_frozen(root, manifest, summary)
    output.write('summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--auth-file', type=Path)
    parser.add_argument('--preflight', type=Path)
    parser.add_argument('--worker', nargs=2)
    parser.add_argument('--followup', action='store_true')
    args = parser.parse_args()
    if args.worker:
        branch, number = args.worker
        if branch not in (*BRANCHES, *FOLLOWUP_CYCLES) or number not in ('1', '2', '3'):
            parser.error('unknown worker session')
        return run_session(args.output, branch, int(number), args.auth_file)
    if not args.source or (args.auth_file and not args.preflight):
        parser.error('source is required, live also requires preflight')
    result = run_series(args.output, args.source, args.auth_file, args.preflight, followup=args.followup)
    returned = sum(row['status'] == 'returned' for row in result['attempts'])
    print(json.dumps(dict(status=result['status'], decisions=result['decisions'], returned_calls=returned,
                          unresolved_attempts=len(result['attempts']) - returned,
                          sessions=[(row['name'], row['status']) for row in result['sessions']])))
    return int(result['status'] != 'completed')


if __name__ == '__main__':
    raise SystemExit(main())
