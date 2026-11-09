"""Прямая сверка конечного D-079: сырые тела, разобранные ответы, память и версии."""
import hashlib
import json
from pathlib import Path

from iam.backup.scan import SecretScanner
from iam.domain import jsonschema
from iam.providers.codex.auth import read_auth
from iam.providers.codex.classify import _json
from iam.providers.codex.provider import _proposals
from iam.state import verify
from iam.storage import Store

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = Path('/Users/azamatsibgatullin/.iam-evidence/s25-appraisal-20261105')
LIVE = PRIVATE/'live-1'


def read(path):
    return _json(Path(path).read_text())


def normalized(proposals):
    return [{key: value for key, value in proposal.items() if key != 'proposal_id'} for proposal in proposals]


def audit(*, check_current=False):
    summary, manifest = read(LIVE/'summary.json'), read(LIVE/'manifest.json')
    assert summary['status'] != 'running'
    assert summary['decisions'] <= 6 and summary['model_calls'] <= 24
    assert summary['model_calls'] == len(summary['attempts'])
    assert manifest['cell_model_calls'] == 4
    assert manifest['native_retries'] == manifest['cycle_retries'] == 0
    assert manifest['image'] == read(PRIVATE/'image.json')['image_id']
    mismatches = []
    for name, digest in manifest['sources'].items():
        for root in ((ROOT, PRIVATE/'frozen') if check_current else (PRIVATE/'frozen',)):
            if hashlib.sha256((root/name).read_bytes()).hexdigest() != digest:
                mismatches.append(str(root/name))
    assert not mismatches, mismatches
    auth = read_auth('/Users/azamatsibgatullin/.iam-secrets/codex-s16-managed-20261030.json', ROOT)
    scanner = SecretScanner(canaries=auth['tokens'].values())
    paths = list(LIVE.rglob('*.json'))
    for path in paths:
        assert not scanner.find(path.read_text()), str(path)
        read(path)
    databases = list(LIVE.rglob('*.db'))
    for path in databases:
        with Store.open(path) as store:
            assert not verify(store) and not store.integrity_problems(), str(path)
    initial_events = {}
    for story, initial in manifest['initial'].items():
        path = LIVE/f'initial/{story}.db'
        assert hashlib.sha256(path.read_bytes()).hexdigest() == initial['sha256']
        with Store.open(path) as store:
            initial_events[story] = [event.to_dict() for event in store.events()]
    totals = dict(physical_bodies=0, native_valid=0, shared_valid=0, provider_valid=0,
                  raw_intention_proposals=0, accepted_intentions=0, appraisals=0,
                  appraisal_emotions=0, raw_reflection_proposals=0)
    cells = []
    for entry in summary['cells']:
        name = entry['name']
        cell = read(LIVE/f'cells/{name}/result.json')
        assert cell['before_digest'] == manifest['initial'][name[0]]['history_digest']
        attempts = [attempt for attempt in summary['attempts'] if attempt['cell'] == name]
        assert len(attempts) == len(cell['traces']) <= 4
        with Store.open(LIVE/f'cells/{name}/persona.db') as store:
            events = [event.to_dict() for event in store.events()]
            prefix = initial_events[name[0]]
            assert events[:len(prefix)] == prefix
            state = store.latest_snapshot().state
            phase_events = [event for event in events if event['type'] == 'proposal.assessed'
                            and event['payload'].get('phase') == 'experience_appraisal_reserved']
        answers = []
        for attempt, trace in zip(attempts, cell['traces']):
            assert attempt['status'] == 'returned'
            physical = read(LIVE/f'cells/{name}/physical/{attempt["index"]:03d}.json')
            assert physical['body_available']
            messages = physical['messages']
            assert hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest() == physical['messages_sha256']
            raw = _json(messages[0])
            totals['physical_bodies'] += 1
            native_errors = jsonschema.check(manifest['response_schema'], raw, Path('.'))
            totals['native_valid'] += int(not native_errors)
            shared_errors = []
            try:
                decoded = _proposals(raw)
            except ValueError:
                decoded = None
                for proposal in raw['proposals']:
                    value = {key: proposal[key] for key in ('kind', 'rationale', 'cites')}
                    value.update(proposal_id='prop_Audit0001', body=_json(proposal['body_json']))
                    shared_errors.extend(jsonschema.errors('proposal', value))
            totals['shared_valid'] += int(decoded is not None)
            kinds = [proposal['kind'] for proposal in raw['proposals']]
            totals['raw_intention_proposals'] += kinds.count('intention')
            totals['raw_reflection_proposals'] += kinds.count('reflection')
            response = trace['response']
            if response is not None:
                assert decoded is not None
                assert not jsonschema.errors('response', response) and not response['tool_use_detected']
                assert normalized(decoded) == normalized(response['proposals'])
                assert all(response[key] == trace['request'][key] for key in ('request_id','epoch','state_version'))
                totals['provider_valid'] += 1
            query = trace['request']
            if query['purpose'] == 'appraise' and query['allowed_proposals'] == ['memory_write','intention']:
                totals['appraisals'] += 1
                markers = [event for event in phase_events if event['payload']['request_id'] == query['request_id']]
                assert markers
                new_sources = {event['payload']['source'] for event in markers}
                assert new_sources <= {item['ref'] for item in query['context']}
                assert all(marker['seq'] < next(event['seq'] for event in events
                    if event['type']=='proposal.received' and event['payload']['response']['request_id']==query['request_id'])
                    for marker in markers)
                if response is not None:
                    for proposal in response['proposals']:
                        assert new_sources.intersection(proposal['cites'])
                        draft = proposal.get('body', {}).get('draft', {})
                        totals['appraisal_emotions'] += int('appraisal' in draft.get('content', {}))
                following = [t for t in cell['traces'] if t['request']['purpose'] == 'choose_action']
                if following:
                    assert following[0]['request']['state_version'] >= query['state_version']
            answers.append(dict(index=attempt['index'], purpose=query['purpose'], kinds=kinds,
                                explanation=raw['explanation'], shared_errors=shared_errors,
                                provider_valid=response is not None))
        accepted = len(cell['new_intentions'])
        assert accepted == entry['new_intentions']
        totals['accepted_intentions'] += accepted
        cells.append(dict(name=name, outcome=entry['outcome'], answers=answers,
                          accepted_intentions=cell['new_intentions'],
                          final_emotion=state['affect'].get('emotion'),
                          final_reflections=len(state['selfmodel'].get('reflections', {})),
                          provider_failure=cell['provider_failure']))
    assert totals['physical_bodies'] == summary['model_calls']
    expected = {f'{story}-{arm}' for story in manifest['stories'] for arm in manifest['arms']}
    return dict(status=summary['status'], decisions=summary['decisions'], model_calls=summary['model_calls'],
                totals=totals, cells=cells, unexecuted_cells=sorted(expected-{cell['name'] for cell in cells}),
                json_files=len(paths), databases=len(databases), sources=len(manifest['sources']),
                source_mismatches=mismatches, checked_source_version='frozen afef471', current_source_comparison=check_current, common_past_prefixes={key:len(value) for key,value in initial_events.items()},
                secret_findings=0, integrity_errors=0, raw_response_mismatches=0,
                readiness=None, full_series_eligible=False,
                limitation='Only A prospective was executed; goal causality unresolved; no repair or rollout.')


if __name__ == '__main__':
    result = audit()
    (ROOT/'docs/evidence/S25-appraisal-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({key:value for key,value in result.items() if key!='cells'}, ensure_ascii=False))
