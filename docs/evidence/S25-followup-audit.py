"""Read-only inventory of one bounded S25 series; never calls a model."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from iam.state import verify
from iam.storage import Store


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root, source):
    manifest = json.loads((root / 'manifest.json').read_text())
    summary = json.loads((root / 'summary.json').read_text())
    report = {'commit': manifest['commit'], 'model': manifest['model'], 'status': summary['status'],
              'decisions': summary['decisions'], 'model_calls': summary['model_calls'],
              'diagnostic': summary.get('diagnostic'), 'errors': [], 'sessions': [], 'json_files': {}, 'databases': {}}
    errors = report['errors']
    for path in sorted(root.rglob('*.json')):
        json.loads(path.read_text())
        report['json_files'][str(path.relative_to(root))] = digest(path)
    for path in sorted(root.rglob('*.db')):
        with Store.open(path, readonly=True) as store:
            item = {'sha256': digest(path), 'kind': store.store_kind, 'integrity': store.integrity_problems(),
                    'replay': verify(store), 'persona_id': store.persona_id}
            if item['kind'] != 'experimental' or item['integrity'] or item['replay']:
                errors.append('database:' + str(path.relative_to(root)))
            report['databases'][str(path.relative_to(root))] = item
    for name, expected in manifest['sources'].items():
        if digest(source / name) != expected:
            errors.append('source:' + name)
    for name, item in manifest['initial'].items():
        if digest(root / item['db']) != item['sha256']:
            errors.append('initial:' + name)
        with Store.open(root / item['db'], readonly=True) as store:
            state = store.latest_snapshot().state
            if state['intentions'] or store.con.execute('SELECT count(*) FROM memory_records').fetchone()[0]:
                errors.append('prepared_experience:' + name)
            if state['preferences']['initial_profile']['motives'] != manifest['profile']['motives']:
                errors.append('profile:' + name)
    if len({x['persona_id'] for p, x in report['databases'].items() if p.startswith('initial/')}) != 5:
        errors.append('initial_identity_count')
    confirmed = Counter()
    intents = Counter()
    purposes = Counter()
    main_confirmed = Counter()
    for session in summary['sessions']:
        name = session['name']
        item = {'name': name, 'main': name in [s + '-' + str(n) for s in 'ABCDE' for n in range(1, 4)],
                'accepted_kinds': Counter(), 'confirmed_capabilities': Counter(), 'intentions': [],
                'purposes': Counter(), 'outcomes': Counter()}
        for path in sorted((root / 'sessions' / name / 'decisions').glob('*.json')):
            decision = json.loads(path.read_text())
            item['outcomes'][decision['report']['outcome']] += 1
            accepted = {e['payload']['proposal_id'] for e in decision['events'] if e['type'] == 'proposal.accepted'}
            for event in decision['events']:
                if event['type'] == 'proposal.requested':
                    item['purposes'][event['payload']['request']['purpose']] += 1
                if event['type'] == 'action.confirmed':
                    item['confirmed_capabilities'][event['payload']['capability_id']] += 1
                if event['type'] != 'proposal.received':
                    continue
                for proposal in event['payload']['response']['proposals']:
                    if proposal['proposal_id'] in accepted:
                        item['accepted_kinds'][proposal['kind']] += 1
                        if proposal['kind'] == 'intention':
                            item['intentions'].append(proposal)
            if decision['technical_faults'] or decision['assessment_errors']:
                errors.append('technical_fault:' + str(path.relative_to(root)))
        if not item['main']:
            checkpoint = json.loads((root / 'checkpoints' / (session['story'] + '-' + str(session['session'] - 1) + '.json')).read_text())
            if session['before_digest'] != checkpoint['history_digest']:
                errors.append('control_history:' + name)
        confirmed.update(item['confirmed_capabilities'])
        if item['main']:
            main_confirmed.update(item['confirmed_capabilities'])
        purposes.update(item['purposes'])
        for proposal in item['intentions']:
            body = proposal['body']
            intents['create' if body['op'] == 'create' else body['to_status']] += 1
        report['sessions'].append(item)
    report.update(confirmed=confirmed, main_confirmed=main_confirmed, intention_operations=intents, purposes=purposes,
                  source_count=len(manifest['sources']), initial_count=len(manifest['initial']))
    if summary['model_calls'] > manifest['budgets']['model_calls'] or summary['decisions'] > manifest['budgets']['decisions']:
        errors.append('budget')
    if len(summary['attempts']) != summary['model_calls']:
        errors.append('physical_attempt_count')
    if summary['manifest_sha256'] != digest(root / 'manifest.json'):
        errors.append('manifest')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = audit(args.root, args.source)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('json_files', 'databases', 'sessions')}, ensure_ascii=False))
