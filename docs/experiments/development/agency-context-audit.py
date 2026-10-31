"""Read-only audit of agency-v1 inputs against the exact original state snapshots."""
from pathlib import Path
import hashlib
import json
import sys

from iam.experiments.agency import CONTRACT, PERSONAL
from iam.storage import Store

root = Path(sys.argv[1]).resolve()
manifest = json.loads((root / 'series-manifest.json').read_text())
assert manifest['runtime'] == 'agency-v1' and manifest['model'] == 'gpt-6-luna'
for name, expected in manifest['execution_sources'].items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, name
checks = []
for path in sorted((root / 'runs').rglob('persona.db')):
    requests = current_requests = 0
    with Store.open(path, readonly=True) as store:
        events = list(store.events())
        starts = [event for event in events if event.type == 'session.started']
        for event in events:
            if event.type != 'proposal.requested':
                continue
            request = event.payload['request']
            state = store.get_snapshot(request['epoch'], request['state_version']).state
            view = json.loads(request['context'][-1]['text'])['development']
            assert request['purpose'] == 'choose_action', 'unexpected mandatory appraisal'
            assert request['context'][0]['text'] == PERSONAL + CONTRACT
            projects = state['world_time']['world']['development']['projects']
            assert view['projects'] == {
                key: {'title': value['title'], 'topic': value['topic'], 'versions': len(value['versions'])}
                for key, value in projects.items()
            }
            # Determine the switch for this original request, not from the final branch:
            # a probe database also contains its full pre-fork biography.
            if 'work_continuity' in view:
                expected = {
                    key: {'versions': len(value['versions']), 'last_two_texts_identical':
                          value['versions'][-1]['text'] == value['versions'][-2]['text']
                          if len(value['versions']) > 1 else None}
                    for key, value in projects.items()
                }
                assert view['work_continuity'] == expected
            else:
                assert not view['interests'] and not view['relationship'] and not view['self_view']
                assert not view['intentions']
                assert not any(item['layer'] in ('personal', 'interpretation') for item in request['context'])
            if path.parent.name == 'memory_off' and event.seq > starts[-1].seq:
                assert 'work_continuity' not in view, 'comparison restored to memory_off'
            requests += 1
            current_requests += event.seq > starts[-1].seq
        per_session = {}
        for event in events:
            if event.type == 'proposal.requested':
                key = event.payload['request']['session_id']
                per_session[key] = per_session.get(key, 0) + 1
        assert max(per_session.values(), default=0) <= 10
    checks.append({'database': str(path.relative_to(root)), 'requests_checked': requests,
                   'last_session_requests': current_requests, 'status': 'pass'})
assert len(checks) == 10, 'two biographies and eight control branches required'
print(json.dumps({'databases_checked': len(checks),
                  'requests_checked_including_copied_past': sum(row['requests_checked'] for row in checks),
                  'execution_sources_unchanged': True, 'checks': checks}, ensure_ascii=False, indent=2))
