"""Календарное окно не подменяет приёмку; аудит читает согласованную копию."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3

from iam.state import EventSpec
from state.helpers import StateCase

_spec = importlib.util.spec_from_file_location(
    'pilot_audit', Path(__file__).resolve().parents[1] / 'tools' / 'pilot_audit.py')
pilot_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pilot_audit)


class PilotAuditTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller()

    def sessions(self, count, gap_days=2):
        ids = []
        for i in range(count):
            sid = f'ses_Pilot{i:08d}'
            self.assertTrue(self.c.start_session(session_id=sid).applied)
            ids.append(sid)
            self.time.advance(1000)
            self.assertTrue(self.c.pause_session().applied)
            self.time.advance(2000)
            self.assertTrue(self.c.resume_session().applied)
            self.time.advance(1000)
            self.assertTrue(self.c.stop_session().applied)
            self.time.advance(gap_days * 86400000)
        return ids

    def test_unassigned_sessions_are_only_statistics(self):
        self.sessions(10)
        result = pilot_audit.audit(self.db)
        self.assertTrue(result['technical_integrity'])
        self.assertEqual(len(result['observed_sessions']), 10)
        self.assertFalse(result['pilot_window_complete'])
        self.assertFalse(result['pilot_complete'])
        self.assertEqual(result['assigned_completed_sessions'], 0)
        self.assertFalse(pilot_audit.audit(self.db, {'session_ids': []})['pilot_window_complete'])

    def test_explicit_calendar_window_uses_journal_not_manifest_order(self):
        ids = self.sessions(10)
        result = pilot_audit.audit(self.db, {'session_ids': list(reversed(ids))})
        self.assertTrue(result['pilot_window_complete'])
        self.assertFalse(result['pilot_complete'])
        self.assertEqual(result['behavioural_acceptance'], 'not_assessed')
        self.assertEqual(result['human_ratings'], 'not_assessed')
        self.assertGreaterEqual(result['calendar_span_ms'], 18 * 86400000)
        first = result['observed_sessions'][0]
        self.assertEqual((first['pauses'], first['resumes']), (1, 1))
        self.assertEqual(first['life_duration_ms'], 2000)
        self.assertEqual(first['calendar_duration_ms'], 4000)
        self.assertEqual(first['started_at'], '2026-10-11T10:00:00.000Z')

    def test_short_window_and_open_session_do_not_qualify(self):
        ids = self.sessions(10, gap_days=0)
        self.assertFalse(pilot_audit.audit(self.db, {'session_ids': ids})['pilot_window_complete'])
        self.assertTrue(self.c.start_session(session_id='ses_LastOpen01').applied)
        result = pilot_audit.audit(self.db, {'session_ids': ['ses_LastOpen01']})
        self.assertEqual(result['assigned_completed_sessions'], 0)
        self.assertIsNone(result['observed_sessions'][-1]['life_duration_ms'])
        self.assertFalse(result['pilot_complete'])

    def test_manifest_rejects_duplicates_unknown_and_malformed_ids(self):
        ids = self.sessions(1)
        for manifest in ({}, [], {'session_ids': 'wrong'}, {'session_ids': ['']},
                         {'session_ids': [5]}, {'session_ids': ids * 2},
                         {'session_ids': ['ses_Unknown001']}, {'session_ids': ids, 'dates': []}):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                pilot_audit.audit(self.db, manifest)

    def test_closed_database_is_not_modified_and_corrupt_copy_is_detected(self):
        self.sessions(2)
        self.c.store.close()
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertTrue(pilot_audit.audit(self.db)['technical_integrity'])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)
        damaged = self.tmp / 'damaged.db'
        shutil.copyfile(self.db, damaged)
        with contextlib.closing(sqlite3.connect(damaged)) as con, con:
            con.execute('DROP TRIGGER state_snapshots_no_delete')
            con.execute('DELETE FROM state_snapshots WHERE state_version = 1')
        result = pilot_audit.audit(damaged)
        self.assertFalse(result['technical_integrity'])
        self.assertTrue(any('нет снимка' in message for message in result['problems']))
        self.assertFalse(result['pilot_complete'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(pilot_audit.main(['--db', str(damaged)]), 1)
        self.assertFalse(json.loads(output.getvalue())['technical_integrity'])

    def test_errors_and_physical_call_ledger_are_reported_without_acceptance(self):
        self.assertTrue(self.c.start_session().applied)
        sid = self.c.session_id
        self.c.record_event(EventSpec('system.error', 'err:test:pilot', {'fault': 'test'}))
        with self.c.store.transaction():
            self.c.store.con.execute('INSERT INTO meta(key,value) VALUES (?,?)',
                                     ('session:' + sid, json.dumps({'attempts': [{}, {}]})))
        result = pilot_audit.audit(self.db)
        self.assertEqual(len(result['system_errors']), 1)
        self.assertEqual(result['observed_sessions'][0]['recorded_physical_calls'], 2)
        self.assertFalse(result['pilot_complete'])

    def test_cli_json_success_and_invalid_input(self):
        self.sessions(1)
        manifest = self.tmp / 'manifest.json'
        manifest.write_text('{"session_ids": []}')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(pilot_audit.main(['--db', str(self.db), '--manifest', str(manifest)]), 0)
        self.assertTrue(json.loads(output.getvalue())['technical_integrity'])
        manifest.write_text('not json')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(pilot_audit.main(['--db', str(self.db), '--manifest', str(manifest)]), 2)
        self.assertIn('error', json.loads(output.getvalue()))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(pilot_audit.main(['--db', str(self.tmp / 'missing.db')]), 2)
        self.assertFalse(json.loads(output.getvalue())['pilot_complete'])
        self.assertFalse((self.tmp / 'missing.db').exists())

    def test_calendar_timezone_is_required(self):
        with self.assertRaises(ValueError):
            pilot_audit.calendar('2026-11-12T10:00:00')

    def test_calendar_rollback_is_visible_and_cannot_qualify(self):
        self.assertTrue(self.c.start_session(session_id='ses_Rollback01').applied)
        self.time.jump_calendar(-86400000)
        self.assertTrue(self.c.stop_session().applied)
        self.assertTrue(self.c.start_session(session_id='ses_Rollback02').applied)
        self.assertTrue(self.c.stop_session().applied)
        result = pilot_audit.audit(self.db, {'session_ids': ['ses_Rollback01', 'ses_Rollback02']})
        self.assertFalse(result['technical_integrity'])
        self.assertTrue(any('внутри сеанса' in item for item in result['problems']))
        self.assertTrue(any('убывают в журнале' in item for item in result['problems']))
        self.assertFalse(result['pilot_window_complete'])

    def test_restoration_is_reported_as_intervention_not_new_session(self):
        from iam.backup import create_backup, restore_backup
        self.sessions(1)
        self.c.store.close()
        backup = create_backup(self.db, self.tmp / 'backups', reason='тест аудита')
        restored = restore_backup(backup.info.path, self.tmp / 'restored', reason='тест восстановления')
        result = pilot_audit.audit(restored.path)
        self.assertTrue(result['technical_integrity'])
        self.assertEqual(len(result['restorations']), 1)
        self.assertEqual(result['restorations'][0]['epoch'], 2)
        self.assertEqual(len(result['observed_sessions']), 1)
        self.assertFalse(result['pilot_complete'])
