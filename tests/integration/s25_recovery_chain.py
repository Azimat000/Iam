"""D-099. Resume interrupted histories and observe the existing path to self-model."""
import argparse
import json
from pathlib import Path

from iam.storage import Store

from integration.intention_probe import snapshot
from integration.live_series import ROOT, StopSeries, sha
from integration.project_affordance import IMAGE, PROJECT_WRITE_IMAGE, events, guard_store, sources
from integration.s25_acceptance import drive

DESIGN = ROOT / 'docs/evidence/S25-recovery-chain-design.md'
REPAIR_DESIGN = ROOT / 'docs/evidence/S25-project-wire-repair-design.md'
REPAIR_BRANCHES = {'shore': ('live/initial/shore.db', 91, 'running', 2)}
REPAIR_HASHES = {
    'live/manifest.json': 'a5d58537efbe074e806b7fb80ba997f68bcb877a28cba87b6195986adc0e413b',
    'live/initial/shore.db': '09d727a97083bf9a38475561d507e7a136638a6b301f82ddcf733bab32188067',
    'live/sessions/shore-1/physical/001.json': '20a0dd91159bc3611ef3781f331dbfc3b75d59a614884ae0f950bc22ff298264',
}
BRANCHES = {
    'shore': ('live/primary/stories/shore/persona.db', 91, 'running', 2),
    'window-1-full': ('live/controls/stories/window-1-full/persona.db', 108, 'running', 1),
    'window-2-full': ('live/controls/stories/window-2-full/persona.db', 89, 'running', 2),
    'cloth': ('repair-live/stories/cloth/persona.db', 116, 'stopped', 2),
}
SOURCE_HASHES = {
    'live/primary/manifest.json': '6d94cebc5672ae9c02b49ac9dcfb782d3542df5aea22a61f1092a4c7f54f9f29',
    'live/primary/summary.json': '89d2b7d50eaccf7c553730dafaac8b4104da3dac8252adeb073c52be51b597d1',
    'live/primary/stories/shore/persona.db': '5bfd6038b5cdcacce1ac93a1144d1383309dc34add20bc47ef57814a4ea9b67b',
    'live/controls/manifest.json': '30d92dba4dd45d38a7bfbd8585469708d10489a9e25ea3ff15a044933586ab0f',
    'live/controls/summary.json': '1aadf55779171f68bcfb38464b4c7c8fdedaefcdf9a6109e45f3f95478c71a42',
    'live/controls/stories/window-1-full/persona.db': '889d71cd3390b7f1746c85cfb2da8052dedded0a4a1aa4c50d069095d2fc78ea',
    'live/controls/stories/window-2-full/persona.db': 'b3ea8251841a5e1caf7684b2c22b8584a6d8c4c8769ead78bd67398eee7b25a2',
    'repair-live/manifest.json': '93a1ed2143a5b2c2799c27f3a7e594edc349f30bc5a542852565a928d05e2c2f',
    'repair-live/summary.json': 'ac2a496767967f0b89b42c73667e911a0736170103399be2137d8ba2882711dd',
    'repair-live/stories/cloth/persona.db': '59aacf3944d7284ce04cf3eecf4e120f52c3aaafc96fcd40cb091bdbe4f514ed',
}


def prepare(root, source, branches, source_hashes):
    if any(sha(source / relative) != digest for relative, digest in source_hashes.items()):
        raise StopSeries('unexpected_recovery_source')
    root.mkdir(mode=0o700)
    initial = {}
    for branch, (relative, seq, status, _) in branches.items():
        original = source / relative
        wal = original.with_suffix('.db-wal')
        if wal.exists() and wal.stat().st_size:
            raise StopSeries('unexpected_recovery_wal')
        path = root / f'initial/{branch}.db'
        snapshot(original, path)
        with Store.open(original, readonly=True) as origin, Store.open(path, readonly=True) as copy:
            guard_store(copy)
            history = events(copy)
            state = copy.latest_snapshot().state
            if history != events(origin) or copy.latest_snapshot() != origin.latest_snapshot():
                raise StopSeries('changed_recovery_history')
            if history[-1]['seq'] != seq or state['world_time']['session_state'] != status:
                raise StopSeries('unexpected_recovery_head')
            world = state['world_time']['world']
            initial[branch] = dict(sha256=sha(path), head=history[-1], materials=list(world.get('library', {})))
            if branch == 'shore':
                goal = state['intentions']['shoreline_evidence_limits']
                if (goal['status'], goal['operation'], goal['created_seq'], goal['project_id']) != (
                        'active', 'write', 87, 'shoreline_evidence_note'):
                    raise StopSeries('unexpected_recovery_goal')
                if goal['project_id'] in world.get('projects', {}):
                    raise StopSeries('shore_already_written')
        snapshot(path, root / f'stories/{branch}/persona.db')
    return initial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('output', 'source', 'auth-file', 'preflight'):
        parser.add_argument('--' + argument, type=Path, required=argument in ('output', 'source'))
    parser.add_argument('--repair', action='store_true')
    args = parser.parse_args()
    root, source = args.output.resolve(), args.source.resolve()
    auth = args.auth_file.expanduser().absolute() if args.auth_file else None
    historical_runs = ('live', 'stub') if args.repair else ('.',)
    if (root.is_relative_to(ROOT) or root == source
            or any(root.is_relative_to(source / path) for path in historical_runs)
            or (auth and auth.resolve().is_relative_to(root))):
        parser.error('external evidence required')
    if auth and not args.preflight:
        parser.error('live requires preflight')
    design, image = (REPAIR_DESIGN, PROJECT_WRITE_IMAGE) if args.repair else (DESIGN, IMAGE)
    branches, source_hashes = (REPAIR_BRANCHES, REPAIR_HASHES) if args.repair else (BRANCHES, SOURCE_HASHES)
    environment = json.loads(args.preflight.read_text()) if auth else None
    if auth and (environment['image'] != image or environment['model_calls'] != 0
                 or not all(environment[key] is True for key in ('isolation_passed', 'schema_matches', 'launcher_matches'))):
        parser.error('preflight failed')
    initial = prepare(root, source, branches, source_hashes)
    limits = {branch: spec[3] for branch, spec in branches.items()}
    interrupted = [branch for branch, spec in branches.items() if spec[2] == 'running']
    result = drive(root, source, initial, environment, auth, sessions=1, max_calls=10 if args.repair else 40,
                   version='D-100' if args.repair else 'D-099', image=image,
                   design=str(design.relative_to(ROOT)), sources=sources(design), source_hashes=source_hashes,
                   interrupted_branches=interrupted, branch_decisions=limits, max_decisions=sum(limits.values()))
    print(json.dumps({key: result[key] for key in ('status', 'decisions')} |
                     {'physical_calls': len(result['attempts'])}, ensure_ascii=False))
    return int(result['status'] != 'completed')


if __name__ == '__main__':
    raise SystemExit(main())
