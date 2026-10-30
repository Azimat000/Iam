#!/usr/bin/env python3
"""Проверить завершённую life-v1 серию и опубликовать только безопасные копии.

PYTHONPATH=src .venv/bin/python scripts/experiments/verify_life.py SOURCE PUBLISH --auth-file EXTERNAL
Ключ и SQLite остаются в SOURCE. SOURCE должен быть отдельным внешним каталогом.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import tempfile

from iam.backup.scan import SecretScanner
from iam.experiments.report.evaluation import outputs
from iam.experiments.report.life import life_packet
from iam.experiments.report.suite import write_csv
from iam.experiments.results import RunResult
from iam.providers.codex.auth import read_auth
from iam.state import verify
from iam.storage import Store

ROOT = Path(__file__).resolve().parents[2]


def check(source, publish, auth_file=None):
    source, publish = Path(source).resolve(), Path(publish).resolve()
    if source.is_relative_to(ROOT) or publish.exists():
        raise ValueError('нужны внешние исходники и новый каталог публикации')
    auth = read_auth(auth_file, ROOT) if auth_file else {}
    scanner = SecretScanner([v for v in auth.get('tokens', {}).values() if isinstance(v, str)], assignments=False)
    summary = json.loads((source / 'summary.json').read_text())
    assert summary['mechanism'] == 'life-v1'
    assert summary['cells'] == len(summary['runs']) == 18
    copied, candidates, pairs, checks = [], [], {}, []
    for row in summary['runs']:
        if row['status'] != 'executed':
            continue
        path = Path(row['trace'])
        assert path.is_relative_to(source)
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row['sha256']
        assert not scanner.find(raw.decode()), 'секрет в трассе'
        trace = json.loads(raw)
        assert trace['architecture'] == row['variant'] + '-life-v1'
        assert trace['status'] == 'completed' and trace['decisions'] == 10
        with Store.open(path.parent / 'persona.db', readonly=True) as store:
            assert store.store_kind == 'experimental'
            assert not verify(store) + store.integrity_problems()
        proposals = {p['proposal_id']: p for p in outputs(trace)}
        arrivals = {e['payload']['input_id']: e['payload']['text'] for e in trace['journal'] if e['type'] == 'input.arrived'}
        seen, progress = set(), {'story': 0, 'patterns': 0}
        for event in trace['journal']:
            p = event['payload']
            if event['type'] == 'input.read':
                assert p['input_id'] not in seen
                seen.add(p['input_id'])
            elif event['type'] == 'proposal.requested':
                context = json.dumps(p['request']['context'], ensure_ascii=False)
                assert all(text not in context for iid, text in arrivals.items() if iid not in seen), 'скрытое чтение'
            elif event['type'] == 'action.confirmed':
                params = proposals[p['proposal_id']]['body']['params']
                if params['operation'] == 'work':
                    progress[params['target']] += 1
        assert progress == trace['final_state']['world_time']['world']['life']['progress']
        assert trace['final_state']['world_time']['life_time_ms'] == 600000
        if row['condition'] == 'memory_off':
            assert trace['memory_records'] == []
            assert all(not d['memory'] for d in trace['decision_trace'])
        if row['condition'] == 'motivation_off':
            assert trace['final_state']['preferences']['life_interests'] == {'story': 0.5, 'patterns': 0.5}
            assert all(not d['motives'] for d in trace['decision_trace'])
        if row['variant'] == 'C':
            pair = pairs.setdefault(row['condition'], [])
            pair.append(raw)
            assert row['calls'] == 0
        if row['condition'] == 'full' and row['done'] == 10 and (summary['mode'] == 'live' or row['variant'] == 'C'):
            candidates.append(RunResult(path.parent, trace))
        checks.append({'cell': row['cell'], 'integrity': 'pass', 'unread': 'pass', 'progress': 'pass',
                       'life_time': 'pass', 'sha256': row['sha256']})
        copied.append((row['cell'], raw))
    assert len(copied) == summary['executed']
    assert all(len(pair) == 2 and pair[0] == pair[1] for pair in pairs.values())
    # Восстановить пакет и ключ побайтово, не раскрывая соответствие эпизодов в выводе.
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        regenerated = life_packet(candidates, base / 'blind', base / 'private/key.json')
        assert len(regenerated) == summary['episodes']
        assert (base / 'private/key.json').read_bytes() == (source / 'private/key.json').read_bytes()
        for path in (base / 'blind').iterdir():
            assert path.read_bytes() == (source / 'blind' / path.name).read_bytes()
    safe_summary = copy.deepcopy(summary)
    for row in safe_summary['runs']:
        if row['trace']:
            row['trace'] = 'traces/' + row['cell'] + '.json'
    pending = {'summary.json': (json.dumps(safe_summary, ensure_ascii=False, indent=2) + '\n').encode()}
    for code, raw in copied:
        pending['traces/' + code + '.json'] = raw
    for path in (source / 'blind').iterdir():
        pending['blind/' + path.name] = path.read_bytes()
    for name, raw in pending.items():
        assert not scanner.find(raw.decode()), 'секрет в публикуемом файле: ' + name
    publish.mkdir(parents=True, exist_ok=False)
    for name, raw in pending.items():
        path = publish / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    write_csv(publish / 'matrix.csv', safe_summary['runs'], list(safe_summary['runs'][0]))
    acceptance = {'databases_checked': len(checks), 'C_identical_pairs': len(pairs),
                  'packet_and_key_reproduced': True, 'secrets_found': 0, 'checks': checks,
                  'human_ratings': None, 'private_files_published': False}
    (publish / 'acceptance.json').write_text(json.dumps(acceptance, ensure_ascii=False, indent=2) + '\n')
    return acceptance


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('publish', type=Path)
    parser.add_argument('--auth-file')
    args = parser.parse_args()
    result = check(args.source, args.publish, args.auth_file)
    print('PASS:', result['databases_checked'], 'баз;', result['C_identical_pairs'], 'одинаковых пар C; секретов 0')
