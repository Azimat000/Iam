"""Refresh rotation survives ephemeral workers without exposing credentials or sharing writers."""
import copy
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from iam.providers.codex import CodexProvider, CodexSandboxConfig, fake, docker_run
from iam.providers.codex.auth import normalize_auth, read_auth
from iam.providers.codex.auth_session import AUTH_EVENT, _read, auth_session, validate_writeback_path
from iam.providers.codex.command import REPO_ROOT
from iam.providers.errors import ProviderAuthError, ProviderTimeout
from iam.providers.run import RunResult
from .test_codex import AUTH, request


def handoff(result, auth):
    return RunResult(result.returncode, result.stdout + '\n' + json.dumps({'type': AUTH_EVENT, 'auth': auth}) + '\n',
                     result.stderr, result.timed_out)


class AuthSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'iam-auth.json'
        self.auth = {**copy.deepcopy(AUTH), 'last_refresh': '2026-10-30T10:00:00Z'}
        self.updated = {**copy.deepcopy(self.auth), 'last_refresh': '2026-10-30T11:00:00Z'}
        self.updated['tokens']['refresh_token'] = 'synthetic-rotated-refresh'
        self.write(self.auth)

    def write(self, auth):
        self.path.write_text(json.dumps(auth))
        self.path.chmod(0o600)

    def provider(self, runner):
        return CodexProvider(runner, CodexSandboxConfig(auth_file=str(self.path), persist_auth_updates=True))

    def test_refresh_persisted_before_format_retry_and_next_request(self):
        payloads = []
        def runner(argv, stdin, *args):
            payloads.append(json.loads(stdin))
            self.assertIn('IAM_AUTH_WRITEBACK=1', argv)
            response = fake.garbage() if len(payloads) == 1 else fake.reply([])
            return handoff(response, self.updated)
        provider = self.provider(runner)
        response = provider.propose(request())
        self.assertEqual(provider.last_calls, 2)
        self.assertEqual(payloads[0]['auth'], self.auth)
        self.assertEqual(payloads[1]['auth'], self.updated)
        self.assertEqual(json.loads(self.path.read_text()), self.updated)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        provider.propose(request())
        self.assertEqual(provider.last_calls, 1)
        self.assertEqual(payloads[2]['auth'], self.updated)
        self.assertNotIn('synthetic-rotated-refresh', json.dumps(response))
        self.assertEqual(list(self.root.glob('.iam-auth-*')), [])
        self.assertNotIn('IAM_AUTH_WRITEBACK=1', docker_run(provider.sandbox, command=['node', '-e', '0']).argv)

    def test_save_on_model_failure_and_tool_attempt_without_leak(self):
        provider = self.provider(lambda *args: handoff(fake.auth_error(), self.updated))
        with self.assertRaises(ProviderAuthError) as caught:
            provider.propose(request())
        self.assertNotIn('synthetic', str(caught.exception))
        self.assertEqual(json.loads(self.path.read_text()), self.updated)
        provider = self.provider(lambda *args: handoff(fake.tool_attempt(), self.updated))
        response = provider.propose(request())
        self.assertTrue(response['tool_use_detected'])
        self.assertEqual(response['proposals'], [])
        self.assertNotIn('refresh_token', json.dumps(response))

    def test_read_only_default_preserves_original_and_ignores_bookkeeping(self):
        native = copy.deepcopy(self.auth)
        native['tokens'].update(login_owner_id='synthetic-owner', oauth_provenance={'source': 'synthetic'})
        self.write(native)
        before = self.path.read_bytes()
        payloads = []
        def runner(argv, stdin, *args):
            payloads.append(json.loads(stdin))
            self.assertNotIn('IAM_AUTH_WRITEBACK=1', argv)
            return fake.reply([])
        CodexProvider(runner, CodexSandboxConfig(auth_file=str(self.path))).propose(request())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(payloads[0]['auth'], AUTH)
        self.assertEqual(normalize_auth(native, include_refresh=True), self.auth)

    def test_missing_malformed_or_untrusted_handoff_never_applies_response(self):
        original = self.path.read_bytes()
        invalid = ['', '\n\n', 'not JSON', 'x' * 65537, '[]', '{}',
                   json.dumps({'type': AUTH_EVENT, 'auth': None}),
                   json.dumps({'type': 'other', 'auth': self.updated}),
                   json.dumps({'type': AUTH_EVENT, 'auth': self.updated, 'extra': True}),
                   '{"type":"iam.auth.updated","type":"iam.auth.updated","auth":{}}']
        for bad_auth in ({**self.updated, 'last_refresh': None}, {**self.updated, 'last_refresh': 'invalid'},
                         {**self.updated, 'last_refresh': '2026-10-30'}, {**self.updated, 'OPENAI_API_KEY': 'synthetic-secret'},
                         {**self.updated, 'tokens': {}}, {**self.updated, 'extra': 'synthetic-secret'}):
            invalid.append(json.dumps({'type': AUTH_EVENT, 'auth': bad_auth}))
        for tail in invalid:
            with self.subTest(tail=tail[:30]):
                provider = self.provider(lambda *args: RunResult(0, fake.reply([]).stdout + tail + '\n'))
                with self.assertRaises(ProviderAuthError) as caught:
                    provider.propose(request())
                self.assertNotIn('synthetic', str(caught.exception))
                self.assertEqual(provider.last_calls, 1)
                self.assertEqual(self.path.read_bytes(), original)
        with auth_session(str(self.path), REPO_ROOT) as session:
            with self.assertRaises(ProviderAuthError):
                session.accept(RunResult(0, ''))

    def test_timeout_requires_pause_and_does_not_trust_partial_handoff(self):
        before = self.path.read_bytes()
        provider = self.provider(lambda *args: handoff(RunResult(None, timed_out=True), self.updated))
        with self.assertRaises(ProviderTimeout):
            provider.propose(request())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.provider(lambda *a: handoff(fake.reply([]), self.auth)).propose(request())['status'], 'ok')

    def test_concurrent_writer_refused_and_lock_released_after_exception(self):
        provider = self.provider(lambda *a: handoff(fake.reply([]), self.auth))
        provider.propose(request())
        with auth_session(str(self.path), REPO_ROOT):
            with self.assertRaises(ProviderAuthError):
                provider.propose(request())
            self.assertEqual(provider.last_calls, 0)
        provider.propose(request())
        self.assertEqual(provider.last_calls, 1)

    def test_external_replacement_preserved(self):
        external = copy.deepcopy(self.auth)
        external['tokens']['access_token'] = 'synthetic-external-change'
        def runner(*args):
            self.write(external)
            return handoff(fake.reply([]), self.updated)
        with self.assertRaises(ProviderAuthError):
            self.provider(runner).propose(request())
        self.assertEqual(json.loads(self.path.read_text()), external)
        self.assertEqual(list(self.root.glob('.iam-auth-*')), [])

    def test_external_replacement_during_atomic_save_preserved(self):
        from iam.providers.codex import auth_session as module
        original_read = module._read
        reads = []
        external = copy.deepcopy(self.auth)
        external['tokens']['access_token'] = 'synthetic-late-change'
        def changed(path, repo):
            reads.append(path)
            if len(reads) == 3:
                self.write(external)
            return original_read(path, repo)
        with patch.object(module, '_read', side_effect=changed), self.assertRaises(ProviderAuthError):
            self.provider(lambda *a: handoff(fake.reply([]), self.updated)).propose(request())
        self.assertEqual(json.loads(self.path.read_text()), external)
        self.assertEqual(list(self.root.glob('.iam-auth-*')), [])

    def test_symlink_hardlink_and_permission_change_after_start_are_not_overwritten(self):
        other = self.root / 'other.json'
        other.write_text('preserve'); other.chmod(0o600)
        for mutation in ('symlink', 'hardlink', 'permissions'):
            self.path.unlink(missing_ok=True); self.write(self.auth)
            def runner(*args):
                if mutation == 'permissions':
                    self.path.chmod(0o644)
                else:
                    self.path.unlink()
                    if mutation == 'symlink': self.path.symlink_to(other)
                    else: os.link(other, self.path)
                return handoff(fake.reply([]), self.updated)
            with self.assertRaises(ProviderAuthError):
                self.provider(runner).propose(request())
            self.assertEqual(other.read_text(), 'preserve')

    def test_private_paths_and_explicit_configuration(self):
        with self.assertRaises(ValueError): validate_writeback_path(None, REPO_ROOT)
        with self.assertRaises(ValueError): CodexSandboxConfig(persist_auth_updates=True)
        with self.assertRaises(ValueError): CodexSandboxConfig(persist_auth_updates=1)
        with patch('pathlib.Path.home', return_value=self.root):
            desktop = self.root / '.codex'; desktop.mkdir(mode=0o700)
            file = desktop / 'auth.json'; file.write_text(json.dumps(self.auth)); file.chmod(0o600)
            with self.assertRaises(ValueError): validate_writeback_path(str(file), REPO_ROOT)
        custom = self.root / 'custom'; custom.mkdir(mode=0o700)
        file = custom / 'auth.json'; file.write_text(json.dumps(self.auth)); file.chmod(0o600)
        with patch.dict(os.environ, {'CODEX_HOME': str(custom)}), self.assertRaises(ValueError):
            validate_writeback_path(str(file), REPO_ROOT)
        self.root.chmod(0o755)
        try:
            with self.assertRaises(ValueError): validate_writeback_path(str(self.path), REPO_ROOT)
        finally: self.root.chmod(0o700)
        with patch('os.getuid', return_value=-1), self.assertRaises(ValueError):
            validate_writeback_path(str(self.path), REPO_ROOT)

    def test_unsafe_lockfile_never_written_and_regular_lock_reusable(self):
        lock = self.path.with_name(self.path.name + '.iam-lock')
        other = self.root / 'preserve'; other.write_text('preserve'); other.chmod(0o600)
        for kind in ('symlink', 'hardlink', 'permissions', 'fifo'):
            if kind == 'symlink': lock.symlink_to(other)
            elif kind == 'hardlink': os.link(other, lock)
            elif kind == 'fifo': os.mkfifo(lock, 0o600)
            else: lock.write_text('preserve'); lock.chmod(0o644)
            with self.assertRaises(ProviderAuthError):
                self.provider(lambda *a: handoff(fake.reply([]), self.auth)).propose(request())
            self.assertEqual(other.read_text(), 'preserve')
            lock.unlink()

    def test_oversized_invalid_duplicate_and_changed_auth_fail_safely(self):
        for raw in (b'x' * 65537, b'not JSON', b'\xff', b'[' * 2000 + b']' * 2000,
                    b'{"auth_mode":"chatgpt","auth_mode":"chatgpt"}'):
            self.path.write_bytes(raw); self.path.chmod(0o600)
            with self.assertRaises(ProviderAuthError):
                with auth_session(str(self.path), REPO_ROOT): self.fail('invalid auth accepted')
        self.write(self.auth)
        info = self.path.stat()
        wrong = SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_nlink=1, st_uid=info.st_uid)
        with patch('os.fstat', return_value=wrong), self.assertRaises(ValueError):
            _read(self.path, REPO_ROOT)

    def test_failed_atomic_write_pauses_without_losing_original(self):
        for target in ('tempfile.mkstemp', 'os.replace', 'os.fsync'):
            self.write(self.auth)
            before = self.path.read_bytes()
            with patch(target, side_effect=OSError('synthetic-secret')), self.assertRaises(ProviderAuthError) as caught:
                self.provider(lambda *a: handoff(fake.reply([]), self.updated)).propose(request())
            self.assertNotIn('synthetic-secret', str(caught.exception))
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(list(self.root.glob('.iam-auth-*')), [])

    def test_directory_sync_failure_stops_even_after_atomic_commit(self):
        fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError('synthetic-secret')
            return fsync(fd)
        with patch('os.fsync', side_effect=fail_directory), self.assertRaises(ProviderAuthError):
            self.provider(lambda *a: handoff(fake.reply([]), self.updated)).propose(request())
        self.assertEqual(json.loads(self.path.read_text()), self.updated)
        self.assertEqual(list(self.root.glob('.iam-auth-*')), [])


if __name__ == '__main__':
    unittest.main()
