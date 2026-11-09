"""D-098. Reopen the two existing incomplete projects on verified experimental copies."""
import argparse
from dataclasses import replace
import json
from pathlib import Path

from iam.domain import MemoryRecord
from iam.state import replay
from iam.storage import Store

from integration.goal_series import load
from integration.intention_probe import snapshot
from integration.live_series import ROOT, StopSeries, sha
from integration.project_affordance import IMAGE, guard_store, sources
from integration.s25_acceptance import drive, initial_record

DESIGN = ROOT / 'docs/evidence/S25-continuity-repair-design.md'
SOURCE_HASHES = {
    'manifest.json': '6d94cebc5672ae9c02b49ac9dcfb782d3542df5aea22a61f1092a4c7f54f9f29',
    'summary.json': '89d2b7d50eaccf7c553730dafaac8b4104da3dac8252adeb073c52be51b597d1',
    'checkpoints/timetable.db': '221567fbca9fecb073b5b35fa7e4fd19ed4cca33701bea4396ed463cc428691c',
    'stories/cloth/persona.db': '15beec19fbd797ebff33688f8e8b34f89f3c0d891fcc682e0b6795fe2313bfbb',
    'sessions/cloth-2/final.json': '6e21469bafcd0262806cb03d4004a9d91e8a6fc83cfd966783becca7a2fede3f',
}
GOALS = {'timetable': 'trip_timing_compare_later', 'cloth': 'record_second_cotton_result'}


def copy_stopped_prefix(source, target, saved):
    expected_head = saved['record']['head']
    with Store.open(source, readonly=True) as origin:
        guard_store(origin)
        history = list(origin.events(to_seq=expected_head['seq']))
        if history[-1].to_dict() != expected_head or history[-1].type != 'session.stopped':
            raise StopSeries('repair_prefix_not_stopped')
        if any(origin.con.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('inbox', 'outbox')):
            raise StopSeries('repair_prefix_has_transport')
        target.parent.mkdir(parents=True, exist_ok=True)
        with Store.create(target, origin.persona_id) as copy:
            for event in history:
                recorded, new = copy.append_event(replace(event, seq=None))
                if not new or recorded != event:
                    raise StopSeries('repair_prefix_event_changed')
            steps = list(replay(copy))
            for epoch, version in steps[-1].snapshots_at:
                copy.put_snapshot(origin.get_snapshot(epoch, version))
            for value in steps[-1].memory.values():
                copy.add_memory(MemoryRecord.from_dict(value))
            guard_store(copy)
            if copy.latest_snapshot().state != saved['state']:
                raise StopSeries('repair_prefix_state_changed')


def run(root, source, auth=None, preflight=None):
    root, source = root.resolve(), source.resolve()
    auth = auth.expanduser().absolute() if auth else None
    if root.is_relative_to(ROOT) or root.is_relative_to(source) or (auth and auth.resolve().is_relative_to(root)):
        raise ValueError('external_evidence_required')
    if any(sha(source/path) != digest for path, digest in SOURCE_HASHES.items()):
        raise StopSeries('unexpected_repair_source')
    for relative in ('checkpoints/timetable.db', 'stories/cloth/persona.db'):
        wal = (source / relative).with_suffix('.db-wal')
        if wal.exists() and wal.stat().st_size:
            raise StopSeries('unexpected_repair_wal')
    environment = json.loads(preflight.read_text()) if auth else None
    if auth and (environment['image'] != IMAGE or environment['model_calls'] != 0
                 or not all(environment[key] is True for key in ('isolation_passed', 'schema_matches', 'launcher_matches'))):
        raise ValueError('preflight_failed')
    root.mkdir(mode=0o700)
    timetable = root / 'initial/timetable.db'
    cloth = root / 'initial/cloth.db'
    snapshot(source/'checkpoints/timetable.db', timetable)
    copy_stopped_prefix(source/'stories/cloth/persona.db', cloth, load(source, 'sessions/cloth-2/final.json'))
    initial = {}
    for branch, goal in GOALS.items():
        path = root / f'initial/{branch}.db'
        initial[branch] = initial_record(path)
        with Store.open(path, readonly=True) as store:
            value = store.latest_snapshot().state['intentions'][goal]
            if value['status'] != 'active' or value['operation'] != 'write':
                raise StopSeries('repair_source_goal_changed')
        snapshot(path, root / f'stories/{branch}/persona.db')
    return drive(root, source, initial, environment, auth, sessions=1, max_calls=20,
                 version='D-098', design=str(DESIGN.relative_to(ROOT)), sources=sources(DESIGN),
                 source_hashes=SOURCE_HASHES, retained_goals=GOALS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('output', 'source', 'auth-file', 'preflight'):
        parser.add_argument('--' + argument, type=Path, required=argument in ('output', 'source'))
    args = parser.parse_args()
    if args.auth_file and not args.preflight:
        parser.error('live requires preflight')
    result = run(args.output, args.source, args.auth_file, args.preflight)
    print(json.dumps({key: result[key] for key in ('status', 'decisions')} |
                     {'physical_calls': len(result['attempts'])}, ensure_ascii=False))
    return int(result['status'] != 'completed')


if __name__ == '__main__':
    raise SystemExit(main())
