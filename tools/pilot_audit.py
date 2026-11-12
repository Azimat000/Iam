#!/usr/bin/env python3
"""Readonly аудит S28. Manifest: {"session_ids": ["ses_..."]}.

Обычные сеансы без явного назначения не засчитываются в пилот. Даты берутся
только из журнала. Календарное окно не является поведенческой приёмкой S28.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from iam.state import verify
from iam.storage import Store, StorageError


def session_ids(manifest):
    if manifest is None:
        return []
    if not isinstance(manifest, dict) or set(manifest) != {'session_ids'}:
        raise ValueError('Manifest должен содержать только session_ids')
    ids = manifest['session_ids']
    if not isinstance(ids, list) or any(not isinstance(sid, str) or not sid.strip() for sid in ids):
        raise ValueError('session_ids должен быть списком непустых строк')
    if len(ids) != len(set(ids)):
        raise ValueError('В manifest повторяются идентификаторы сеансов')
    return ids


def calendar(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Календарная дата журнала не содержит часовой пояс')
    return result


def audit(db, manifest=None):
    assigned = session_ids(manifest)
    with Store.open(db, readonly=True) as store:
        # Один согласованный снимок даже при работающем отдельном писателе.
        store.con.execute('BEGIN')
        problems = verify(store)
        events = list(store.events())
        sessions = {}
        anomalies, restorations = [], []
        for event in events:
            if event.type == 'system.restored':
                restorations.append({'seq': event.seq, 'epoch': event.epoch})
            if event.type == 'system.error':
                anomalies.append({'seq': event.seq, 'type': event.type})
            if event.type == 'session.started':
                sid = event.payload['session_id']
                if sid in sessions:
                    problems.append('Повтор начала сеанса: ' + sid)
                sessions[sid] = {'session_id': sid, 'started_at': event.occurred_at,
                                 'started_seq': event.seq, 'start_life_ms': event.life_time_ms,
                                 'stopped_at': None, 'end_life_ms': None, 'pauses': 0,
                                 'resumes': 0, 'epoch': event.epoch}
            elif event.type in ('session.paused', 'session.resumed', 'session.stopped'):
                sid = event.payload['session_id']
                if sid not in sessions:
                    problems.append('Событие без начала сеанса: ' + sid)
                    continue
                entry = sessions[sid]
                if event.type == 'session.stopped':
                    entry.update(stopped_at=event.occurred_at, end_life_ms=event.life_time_ms)
                else:
                    entry['pauses' if event.type == 'session.paused' else 'resumes'] += 1
        missing = set(assigned) - sessions.keys()
        if missing:
            raise ValueError('Неизвестные сеансы manifest: ' + ', '.join(sorted(missing)))
        for sid, entry in sessions.items():
            ledger = store.con.execute('SELECT value FROM meta WHERE key=?', ('session:' + sid,)).fetchone()
            entry['recorded_physical_calls'] = len(json.loads(ledger[0])['attempts']) if ledger else None
            entry['life_duration_ms'] = (entry['end_life_ms'] - entry['start_life_ms']
                                         if entry['end_life_ms'] is not None else None)
            entry['calendar_duration_ms'] = (int((calendar(entry['stopped_at']) - calendar(entry['started_at'])).total_seconds() * 1000)
                                             if entry['stopped_at'] else None)
            if entry['calendar_duration_ms'] is not None and entry['calendar_duration_ms'] < 0:
                problems.append('Календарное время убывает внутри сеанса: ' + sid)
        selected = [entry for sid, entry in sessions.items() if sid in assigned]
        dates = [calendar(entry['started_at']) for entry in selected]
        if dates != sorted(dates):
            problems.append('Календарные даты назначенных сеансов убывают в журнале')
        span_ms = int((max(dates) - min(dates)).total_seconds() * 1000) if dates else 0
        completed = sum(entry['stopped_at'] is not None for entry in selected)
        snapshot = store.latest_snapshot()
        requests = snapshot.state['world_time'].get('world', {}).get('creator_requests', {}) if snapshot else {}
        return {'technical_integrity': not problems, 'problems': problems,
                'observed_sessions': list(sessions.values()), 'assigned_session_ids': assigned,
                'assigned_completed_sessions': completed, 'calendar_span_ms': span_ms,
                'pilot_window_complete': not problems and completed >= 10 and completed == len(selected)
                                         and span_ms >= 14 * 86400000,
                'pilot_complete': False, 'behavioural_acceptance': 'not_assessed',
                'human_ratings': 'not_assessed', 'system_errors': anomalies, 'restorations': restorations,
                'creator_request_statuses': {key: value.get('status') for key, value in requests.items()},
                'limitations': ['Назначение сеансов manifest должно подтверждаться протоколом до пилота.',
                                'Причины ошибок и допустимость ожиданий требуют отдельного разбора.',
                                'Окно 10/14 не подтверждает поведение, оценки пользователя или приёмку S28.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text()) if args.manifest else None
        report = audit(args.db, manifest)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, StorageError) as error:
        print(json.dumps({'error': str(error), 'technical_integrity': False, 'pilot_complete': False}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['technical_integrity'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
