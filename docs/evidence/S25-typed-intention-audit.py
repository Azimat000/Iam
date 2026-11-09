"""Независимая от стенда сверка raw, ответов, общего прошлого и неизменности кода D-078."""
import hashlib
import json
from pathlib import Path
import re

from iam.backup.scan import SecretScanner
from iam.domain import jsonschema
from iam.providers.codex.auth import read_auth
from iam.providers.codex.command import RESPONSE_JSON_SCHEMA
from iam.providers.codex.classify import _json
from iam.providers.codex.provider import _proposals
from iam.state import StateController, verify
from iam.storage import Store
from integration import intention_probe as probe, typed_intention as typed

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path('/Users/azamatsibgatullin/.iam-evidence/s25-typed-20261105')
LIVE = EVIDENCE / 'live-matrix-1'
FROZEN = EVIDENCE / 'frozen'


def read(path):
    return _json(Path(path).read_text())


def normalized(items):
    return [{key: value for key, value in item.items() if key != 'proposal_id'} for item in items]


def audit():
    summary, manifest = read(LIVE/'summary.json'), read(LIVE/'manifest.json')
    assert summary['status'] != 'running'
    assert summary['decisions'] <= 6 and summary['model_calls'] <= 12
    assert summary['model_calls'] == len(summary['attempts'])
    assert manifest['native_retries'] == manifest['cycle_retries'] == 0
    assert manifest['model'] == 'gpt-6-luna'
    assert manifest['typed_schema'] == typed.SCHEMA and manifest['typed_prompt'] == typed.PROMPT
    image = read(EVIDENCE/'image.json')
    assert manifest['image'] == image['image_id']
    source_mismatches = []
    for name, digest in manifest['sources'].items():
        for root in (ROOT, FROZEN):
            if hashlib.sha256((root/name).read_bytes()).hexdigest() != digest:
                source_mismatches.append(str(root/name))
    assert not source_mismatches, source_mismatches
    credentials = read_auth('/Users/azamatsibgatullin/.iam-secrets/codex-s16-managed-20261030.json', ROOT)
    scanner = SecretScanner(canaries=credentials['tokens'].values())
    json_count = 0
    for path in LIVE.rglob('*.json'):
        data = path.read_text()
        assert not scanner.find(data), str(path)
        _json(data)
        json_count += 1
    db_count = 0
    for path in LIVE.rglob('*.db'):
        with Store.open(path) as store:
            assert not verify(store) and not store.integrity_problems(), str(path)
        db_count += 1
    for story, initial in manifest['initial'].items():
        path = LIVE/f'initial/{story}.db'
        assert probe.series.sha(path) == initial['sha256']
        assert probe.series.state_digest(path) == initial['history_digest']
    rows = []
    totals = dict(physical_bodies=0, unavailable_bodies=0, explicit_objects=0, explicit_nulls=0,
                  legacy_intention_proposals=0, reflection_proposals=0,
                  native_valid_answers=0, provider_valid_answers=0, accepted_intentions=0)
    for cell_summary in summary['cells']:
        name = cell_summary['name']
        cell = read(LIVE/f'cells/{name}/result.json')
        assert cell['before_digest'] == manifest['initial'][name[0]]['history_digest']
        attempts = [attempt for attempt in summary['attempts'] if attempt['cell'] == name]
        assert len(attempts) <= 2 and len(cell['traces']) == len(attempts)
        raw_rows = []
        for attempt, trace in zip(attempts, cell['traces']):
            assert attempt['status'] == 'returned'
            record = read(LIVE/f'cells/{name}/physical/{attempt["index"]:03d}.json')
            messages = record['messages']
            assert hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest() == record['messages_sha256']
            totals['physical_bodies'] += int(record['body_available'])
            totals['unavailable_bodies'] += int(not record['body_available'])
            assert record['body_available']
            raw = _json(messages[0])
            is_typed = name.split('-', 1)[1] != 'baseline' and trace['request']['purpose'] == 'choose_action'
            schema = typed.SCHEMA if is_typed else RESPONSE_JSON_SCHEMA
            valid = not jsonschema.check(schema, raw, Path('.'))
            totals['native_valid_answers'] += int(valid)
            if is_typed:
                assert 'future_intention' in raw
                totals['explicit_objects'] += int(isinstance(raw['future_intention'], dict))
                totals['explicit_nulls'] += int(raw['future_intention'] is None)
            kinds = [item['kind'] for item in raw['proposals']]
            totals['legacy_intention_proposals'] += kinds.count('intention')
            totals['reflection_proposals'] += kinds.count('reflection')
            response = trace['response']
            if response is not None:
                assert trace['error'] is None and not response['tool_use_detected']
                assert not jsonschema.errors('response', response)
                expected = _proposals(typed.convert(raw) if is_typed else raw)
                assert normalized(expected) == normalized(response['proposals'])
                assert all(response[key] == trace['request'][key] for key in ('request_id', 'epoch', 'state_version'))
                totals['provider_valid_answers'] += 1
            if name.endswith('memory_off'):
                request = trace['request']
                assert not re.findall(r'mem_[A-Za-z0-9]{8,40}', json.dumps(request['context']))
                assert all(item['layer'] not in ('personal', 'interpretation') for item in request['context'])
            raw_rows.append(dict(index=attempt['index'], kinds=kinds,
                                 future_intention=raw.get('future_intention', 'field_not_in_baseline'),
                                 native_schema_valid=valid, provider_valid=response is not None,
                                 explanation=raw['explanation']))
        count = len(cell['new_intentions'])
        assert count == cell_summary['new_intentions']
        totals['accepted_intentions'] += count
        rows.append(dict(name=name, answers=raw_rows, accepted=cell['new_intentions'],
                         outcome=cell_summary['outcome'], provider_failure=cell['provider_failure']))
    assert totals['physical_bodies'] + totals['unavailable_bodies'] == summary['model_calls']
    return dict(version=manifest['version'], status=summary['status'], decisions=summary['decisions'],
                model_calls=summary['model_calls'], totals=totals, cells=rows, json_files=json_count,
                databases=db_count, source_files=len(manifest['sources']), source_mismatches=source_mismatches,
                state_integrity_errors=0, secret_findings=0, raw_response_mismatches=0,
                common_initial_history=True, memory_off_checked=True, image_id=manifest['image'],
                preflight_configuration_rejected_before_any_call=read(EVIDENCE/'preflight-setup.json'),
                readiness=None, scope='Diagnostic only; prepared mechanical cases are not live behavior.')


if __name__ == '__main__':
    result = audit()
    (ROOT/'docs/evidence/S25-typed-intention-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'cells'}, ensure_ascii=False))
