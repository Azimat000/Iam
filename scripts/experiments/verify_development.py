#!/usr/bin/env python3
"""Проверить развитие/контрольные ветви и опубликовать безопасные копии, без SQLite/ключа."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import tempfile

from iam.backup.scan import SecretScanner
from iam.experiments.conversation import records
from iam.experiments.development import AXES, experience_text
from iam.experiments.report.development import assess_development, development_packet
from iam.experiments.report.suite import write_csv
from iam.experiments.results import Normalizer, RunResult
from iam.providers.codex.auth import read_auth
from iam.state import StateController, verify
from iam.storage import Store

ROOT = Path(__file__).resolve().parents[2]


def canonical_source_metadata(trace, names):
    """Сравнение C: меняются только технические ключи таблицы ядра, исходная трасса сохраняется."""
    def convert(value):
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: convert(item) for key, item in value.items()}
        if (result.get('origin') == 'simulation_event' and result.get('layer') == 'state'
                and isinstance(result.get('text'), str) and result['text'].startswith('{"experience_sources":')):
            meta = json.loads(result['text'])
            assert set(meta) == {'experience_sources', 'appraisal_kinds'}
            assert set(meta['experience_sources']) <= names.keys(), 'неизвестная техническая ссылка'
            meta['experience_sources'] = {names[key]: item for key, item in meta['experience_sources'].items()}
            result['text'] = json.dumps(meta, ensure_ascii=False, sort_keys=True)
        return result
    return convert(trace)


def choices(trace):
    """Различаем поступки, не считая другие формулировки речи доказательством причины."""
    world = trace['final_state']['world_time']['world'].get('actions', {})
    signature, messages = [], []
    for decision in trace['decision_trace']:
        current = []
        for action in decision['report']['actions']:
            if action['status'] != 'confirmed': continue
            stored = world[action['action_id']]
            if stored['kind'] == 'message':
                current.append({'kind': 'message', 'to': stored['to']})
                messages.append(stored['text'])
            else:
                params = stored['params']
                current.append({'kind': 'action', 'operation': params['operation'],
                                'target': params.get('topic') if params['operation'] == 'write_project' else params['target']})
        signature.append(current if current else [{'outcome': decision['report']['outcome']}])
    return {'choices': signature, 'messages': messages}


def verify_trace(path, expected_hash):
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected_hash, 'неверный SHA256'
    trace = json.loads(raw)
    with Store.open(path.parent / 'persona.db', readonly=True) as store:
        assert store.store_kind == 'experimental'
        assert not verify(store) + store.integrity_problems(), 'повреждённая база'
        controller = StateController(store)
        memory = {r.memory_id: r for r in records(controller)}
        events = controller.events_after(0)
        event_map = {e.event_id: e for e in events}
        arrived = {e.payload['input_id']: e.payload['text'] for e in events if e.type == 'input.arrived'}
        read = set()
        for event in events:
            if event.type == 'input.read':
                read.add(event.payload['input_id'])
            elif event.type == 'proposal.requested':
                context = json.dumps(event.payload['request']['context'], ensure_ascii=False)
                assert all(text not in context for iid, text in arrived.items() if iid not in read), 'скрытое чтение'
        for record in memory.values():
            if record.origin != 'own_hypothesis': continue
            c = record.content
            source = c['source_ref']
            if source in memory:
                basis = memory[source]
                assert basis.origin in ('observation', 'stated_by_other', 'simulation_event')
                text = basis.text
                social = basis.source.kind == 'external_person'
            else:
                event = event_map[source]
                if event.type == 'input.read':
                    text = arrived[event.payload['input_id']]
                    social = True
                else:
                    assert event.type == 'action.confirmed'
                    text, social = event.payload['summary'], False
                    if trace['scenario'] == 'development_v4':
                        action = controller.state['world_time']['world']['actions'][event.payload['action_id']]
                        text = experience_text(action['params'], text)
            assert c['quote'] in text and c['quote'].strip()
            if c['domain'] == 'relationship': assert social and c['key'] == 'operator'
            if c['domain'] == 'self' and trace['scenario'] == 'development_v4':
                basis = record.sources[0]
                event = event_map[basis]
                assert event.type == 'action.confirmed'
                action = controller.state['world_time']['world']['actions'][event.payload['action_id']]
                params = action['params']
                assert action['status'] == 'confirmed' and params['operation'] in ('write_project', 'open_project')
                receipt = 'Подтверждено действие Аси: ' + params['operation'] + ', цель: ' + params['target'] + '.'
                assert c['quote'] == receipt and c['key'] == 'identity' and record.kind == 'autobiographical'
            previous = memory[record.links[0].target].content['values'] if record.links else {}
            axes = {'strength': (-1.0, 1.0, 0.0)} if c['domain'] == 'interest' else AXES if c['domain'] == 'relationship' else {}
            for axis, (lo, hi, default) in axes.items():
                value = c['values'][axis]
                assert lo <= value <= hi and abs(value - previous.get(axis, default)) <= 0.250001
        actual = store.latest_snapshot().state
        world = actual['world_time']['world']['development']
        artifacts = []
        for key, project in world['projects'].items():
            for n, version in enumerate(project['versions'], 1):
                path_ = path.parent / 'artifacts' / (version['action_id'] + '.txt')
                assert path_.read_text() == version['text'], 'нет подтверждённой работы'
                artifacts.append((key + '-v' + str(n) + '.txt', path_.read_bytes()))
        for group, path_ in (('preferences', 'development_interests'), ('relations', 'operator'), ('selfmodel', 'development')):
            value = actual[group].get(path_, {})
            entries = value.values() if group == 'preferences' else [value] if value else []
            for entry in entries:
                assert len(entry['bases']) == len(set(entry['bases']))
                assert len(entry['basis_tokens']) == len(set(entry['basis_tokens']))
        assert actual['traits'] == trace['persona']['traits']
    for d in trace['decision_trace']:
        for request in d['requests']:
            contexts = request['context']
            view = json.loads(contexts[-1]['text'])['development']
            off = trace['switches']
            if off['memory_off']:
                assert not any(c['layer'] in ('personal', 'interpretation') for c in contexts)
                assert view['interests'] == view['relationship'] == view['self_view'] == view['intentions'] == {}
            if off['interests_off']: assert not view['interests']
            if off['relationships_off']: assert not view['relationship']
            for c in contexts:
                record = next((r for r in trace['memory_records'] if r['memory_id'] == c['ref']), None)
                if record:
                    assert record['layer'] != 'archive'
                    domain = record['content'].get('domain')
                    assert not (off['interests_off'] and domain == 'interest')
                    assert not (off['relationships_off'] and domain == 'relationship')
    return trace, raw, artifacts


def check(source, publish, auth_file=None):
    source, publish = Path(source).resolve(), Path(publish).resolve()
    if source.is_relative_to(ROOT) or publish.exists(): raise ValueError('нужны внешние исходники и новый каталог публикации')
    auth = read_auth(auth_file, ROOT) if auth_file else {}
    scanner = SecretScanner([v for v in auth.get('tokens', {}).values() if isinstance(v, str)], assignments=False)
    summary = json.loads((source / 'summary.json').read_text())
    assert summary['mechanism'] in ('development-v1', 'development-v2', 'development-v3', 'development-v4')
    assert len(summary['runs']) == 2 * len(summary.get('variants', ['A', 'B', 'C'])) * summary['repeats']
    safe = copy.deepcopy(summary)
    pending, results, checks, c_pairs, comparisons = {}, [], [], {}, []
    for original, row in zip(summary['runs'], safe['runs']):
        if original['status'] != 'executed':
            assert original['reason']
            continue
        path = Path(original['trace'])
        assert path.resolve().is_relative_to(source)
        trace, raw, artifacts = verify_trace(path, original['sha256'])
        assert trace['decisions'] == 18 and len(trace['sessions']) == 6
        assert all(s['restored'] for s in trace['sessions'])
        assert trace['final_state']['world_time']['life_time_ms'] == 18 * 60000
        assessment = assess_development(trace)
        assert assessment['structural_admission'] == original['structural_admission']
        name = 'traces/' + row['cell'] + '.json'
        pending[name], row['trace'] = raw, name
        pending['assessments/' + row['cell'] + '.json'] = (json.dumps(assessment, ensure_ascii=False, indent=2) + '\n').encode()
        pending['biographies/' + row['cell'] + '.md'] = (path.parent / 'biography.md').read_bytes()
        for filename, content in artifacts: pending['artifacts/' + row['cell'] + '/' + filename] = content
        if summary['mode'] == 'live' or row['variant'] == 'C': results.append(RunResult(path.parent, trace))
        if row['variant'] == 'C':
            with Store.open(path.parent / 'persona.db', readonly=True) as store:
                normal = Normalizer()
                normal([e.to_dict() for e in StateController(store).events_after(0)])
            c_pairs.setdefault(row['history'], []).append(canonical_source_metadata(trace, normal.names))
        checks.append({'cell': row['cell'], 'kind': 'training', 'sha256': original['sha256'], 'integrity': 'pass'})
        observed = {}
        for condition, branch in original['probes'].items():
            probe_path = Path(branch['trace'])
            assert probe_path.resolve().is_relative_to(source)
            probe, probe_raw, probe_artifacts = verify_trace(probe_path, branch['sha256'])
            assert probe['journal'][:len(trace['journal'])] == trace['journal'], 'ветви имеют разное прошлое'
            assert probe['final_state']['world_time']['life_time_ms'] == 20 * 60000
            assert probe['sessions'][0]['restored']
            observed[condition] = choices(probe)
            probe_name = 'probes/' + row['cell'] + '-' + condition + '.json'
            pending[probe_name] = probe_raw
            row['probes'][condition]['trace'] = probe_name
            checks.append({'cell': row['cell'], 'kind': condition, 'sha256': branch['sha256'], 'integrity': 'pass'})
        comparisons.append({'cell': row['cell'], 'conditions': observed,
            'vs_full': {condition: {'choices_differ': value['choices'] != observed['full']['choices'],
                                    'speech_differs': value['messages'] != observed['full']['messages']}
                        for condition, value in observed.items() if condition != 'full'},
            'causal_effect_established': False,
            'limitation': 'Одной пробы и различия текста недостаточно: у модели нет гарантированного детерминизма. '
                          'Удаляется явный компонент, эпизодический опыт остаётся; нужны смысловые основания и повторные проверки.'})
    assert len([c for c in checks if c['kind'] == 'training']) == summary['executed']
    if summary['repeats'] == 2: assert all(len(p) == 2 and p[0] == p[1] for p in c_pairs.values())
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        mapping = development_packet(results, temp / 'blind', temp / 'private/key.json')
        assert len(mapping) == summary['human_packet']
        assert (temp / 'private/key.json').read_bytes() == (source / 'private/key.json').read_bytes()
        for p in (temp / 'blind').iterdir():
            assert p.read_bytes() == (source / 'blind' / p.name).read_bytes()
            pending['blind/' + p.name] = p.read_bytes()
    pending['summary.json'] = (json.dumps(safe, ensure_ascii=False, indent=2) + '\n').encode()
    pending['probe-comparison.json'] = (json.dumps(comparisons, ensure_ascii=False, indent=2) + '\n').encode()
    for name, raw in pending.items(): assert not scanner.find(raw.decode()), 'секрет: ' + name
    publish.mkdir(parents=True, exist_ok=False)
    for name, raw in pending.items():
        path = publish / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    write_csv(publish / 'matrix.csv', safe['runs'], list(safe['runs'][0]))
    acceptance = {'databases_checked': len(checks), 'checks': checks, 'unread_verified': True,
                  'shared_checkpoint_verified': True, 'artifacts_verified': True, 'packet_reproduced': True,
                  'C_identical_pairs': len(c_pairs) if summary['repeats'] == 2 else 0,
                  'C_comparison': 'Нормализованы только технические ссылки в служебной таблице источников; исходные трассы/SHA256 сохранены.',
                  'secrets_found': 0, 'human_ratings': None, 'private_files_published': False}
    (publish / 'acceptance.json').write_text(json.dumps(acceptance, ensure_ascii=False, indent=2) + '\n')
    return acceptance


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('publish', type=Path)
    parser.add_argument('--auth-file')
    args = parser.parse_args()
    result = check(args.source, args.publish, args.auth_file)
    print('PASS:', result['databases_checked'], 'баз, общее прошлое/источники/работы проверены, секретов 0')
