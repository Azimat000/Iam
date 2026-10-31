"""Codex bootstrap and report failures without Docker or paid model requests."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from iam.providers.codex import CodexSandboxConfig, RESPONSE_JSON_SCHEMA
from iam.domain import jsonschema
from iam.providers.run import RunResult

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('codex_verify', ROOT / 'scripts/codex/verify.py')
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)
AUTH = {'auth_mode': 'chatgpt', 'tokens': {key: 'synthetic-value' for key in ('access_token', 'id_token', 'refresh_token')}}


class VerificationTest(unittest.TestCase):
    def runner(self, argv, stdin, timeout, cleanup):
        self.assertEqual(stdin, '')
        if argv[1:3] == ('network', 'inspect'):
            return RunResult(0, '[{"Internal":true}]')
        self.assertIn('--read-only', argv)
        self.assertIn('--cap-drop', argv)
        self.assertNotIn('--mount', argv)
        self.assertNotIn('--env-file', argv)
        self.assertEqual(cleanup[:2], ('docker', 'kill'))
        if verify.DIAGNOSTIC in argv:
            return RunResult(0, json.dumps(dict.fromkeys(verify.REQUIRED[2:-3], True)))
        if 'curl' in argv:
            if '--noproxy' in argv:
                return RunResult(7, '000')
            if 'https://example.com' in argv:
                return RunResult(56, '403')
            return RunResult(0, '200')
        return RunResult(0)

    def test_schema_is_identical_and_report_requires_all_checks(self):
        self.assertEqual(json.loads((ROOT / 'docker/response.schema.json').read_text()), RESPONSE_JSON_SCHEMA)
        self.assertEqual(jsonschema.errors('request', verify.REQUEST), [])
        self.assertTrue(all(item['ref'].startswith(('evt_', 'mem_')) for item in verify.REQUEST['context']))
        results = verify.collect(CodexSandboxConfig(), runner=self.runner)
        self.assertTrue(verify.verdict(results))
        self.assertEqual(results['live'], 'UNVERIFIED')
        self.assertFalse(verify.verdict(results, True))
        for key in verify.REQUIRED:
            bad = dict(results)
            del bad[key]
            self.assertFalse(verify.verdict(bad), key)
            bad[key] = 'FAIL'
            self.assertFalse(verify.verdict(bad), key)

    def test_dead_container_and_malformed_output_never_pass(self):
        for result in (RunResult(127, '', 'synthetic-secret'), RunResult(0, 'broken'),
                       RunResult(0, 'null'), RunResult(0, '{}'), RunResult(None, '', '', True)):
            results = verify.collect(CodexSandboxConfig(), runner=lambda *args: result)
            self.assertFalse(verify.verdict(results))
            self.assertEqual(results['non_root'], 'FAIL')
            self.assertEqual(results['network_internal'], 'FAIL')
            self.assertNotIn('synthetic-secret', json.dumps(results))

    def test_schema_read_errors_fail(self):
        for failure in (OSError(), ValueError()):
            with patch.object(Path, 'read_text', side_effect=failure):
                results = verify.collect(CodexSandboxConfig(), runner=self.runner)
            self.assertEqual(results['schema'], 'FAIL')

    def test_live_only_after_isolation_and_one_call(self):
        calls = []
        class Provider:
            def __init__(self, runner, cfg, **kwargs):
                self_kwargs = kwargs
                calls.append(self_kwargs)
            def propose(self, request):
                return {'provider': {'model': 'gpt-6-luna'}, 'tool_use_detected': False,
                        'proposals': [{'kind': 'no_action'}]}
        results = verify.collect(CodexSandboxConfig(), live=True, runner=self.runner, provider_cls=Provider)
        self.assertTrue(verify.verdict(results, True))
        self.assertEqual(calls, [{'max_format_retries': 0}])
        results = verify.collect(CodexSandboxConfig(), live=True, runner=lambda *a: RunResult(127), provider_cls=Provider)
        self.assertFalse(verify.verdict(results, True))
        self.assertEqual(len(calls), 1)
        for response in ({}, {'provider': {'model': 'other'}, 'tool_use_detected': False, 'proposals': []},
                         {'provider': {'model': 'gpt-6-luna'}, 'tool_use_detected': True, 'proposals': []},
                         {'provider': {'model': 'gpt-6-luna'}, 'tool_use_detected': False, 'proposals': []},
                         {'provider': {'model': 'gpt-6-luna'}, 'tool_use_detected': False, 'proposals': [{'kind':'action'}]}):
            with patch.object(Provider, 'propose', return_value=response):
                results = verify.collect(CodexSandboxConfig(), live=True, runner=self.runner, provider_cls=Provider)
            self.assertEqual(results['live'], 'FAIL')

    def test_main_paths_auth_reports_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / 'out')
            auth = Path(directory) / 'auth.json'
            auth.write_text(json.dumps(AUTH))
            auth.chmod(0o600)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                for args in (['--output-dir', '.'], ['--output-dir', str(ROOT / 'out')],
                             ['--output-dir', output, '--live'],
                             ['--output-dir', output, '--persist-auth-updates'],
                             ['--output-dir', output, '--auth-file', str(auth) + '-missing'],
                             ['--output-dir', str(Path(directory) / '..' / 'out')]):
                    self.assertEqual(verify.main(args), 2)
                output_path = Path(output)
                output_path.mkdir()
                report_path = output_path / 'report.json'
                report_path.symlink_to(auth)
                self.assertEqual(verify.main(['--output-dir', output]), 2)
                report_path.unlink()
                report_path.write_text(json.dumps(AUTH))
                report_path.chmod(0o600)
                self.assertEqual(verify.main(['--output-dir', output, '--auth-file', str(report_path)]), 2)
                self.assertEqual(json.loads(report_path.read_text()), AUTH)
                passed = dict.fromkeys(verify.REQUIRED, 'PASS') | {'live': 'UNVERIFIED'}
                with patch.object(verify, 'collect', return_value=passed):
                    self.assertEqual(verify.main(['--output-dir', output, '--auth-file', str(auth)]), 0)
                report = json.loads((Path(output) / 'report.json').read_text())
                self.assertTrue(report['isolation_passed'])
                self.assertNotIn('synthetic-value', json.dumps(report))
                managed = Path(directory) / 'iam-managed.json'
                managed.write_text(json.dumps(AUTH)); managed.chmod(0o600)
                with patch.object(verify, 'collect', return_value=passed | {'live': 'PASS'}) as collect:
                    self.assertEqual(verify.main(['--output-dir', output, '--auth-file', str(managed), '--live', '--persist-auth-updates']), 0)
                    self.assertTrue(collect.call_args.args[0].persist_auth_updates)
                    self.assertTrue(json.loads((Path(output) / 'report.json').read_text())['auth_updates_enabled'])
                with patch.object(verify, 'collect', return_value=passed | {'live': 'FAIL'}):
                    self.assertEqual(verify.main(['--output-dir', output, '--auth-file', str(auth), '--live']), 1)
                with patch.object(verify, 'collect', return_value=passed), patch.object(Path, 'write_text', side_effect=OSError()):
                    self.assertEqual(verify.main(['--output-dir', output]), 2)
                with patch.object(Path, 'mkdir', side_effect=OSError()):
                    self.assertEqual(verify.main(['--output-dir', output]), 2)

    def test_report_hardlink_cannot_overwrite_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / 'auth.json'
            auth.write_text(json.dumps(AUTH))
            auth.chmod(0o600)
            output = root / 'out'
            output.mkdir()
            os.link(auth, output / 'report.json')
            original = auth.read_bytes()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(verify.main(['--auth-file', str(auth), '--output-dir', str(output)]), 2)
            self.assertEqual(auth.read_bytes(), original)


@unittest.skipUnless(shutil.which('node'), 'Node required for bootstrap checks')
class BootstrapTest(unittest.TestCase):
    def run_bootstrap(self, payload, command=None, existing=False, writeback=False):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            if existing:
                home.mkdir()
                (home / 'auth.json').write_text('preserve')
            done = subprocess.run(['node', str(ROOT / 'docker/launch.cjs'), *(command or [])],
                                  input=payload, capture_output=True, text=True,
                                  env={**os.environ, 'CODEX_HOME': str(home), 'IAM_AUTH_WRITEBACK': '1' if writeback else '0'}, timeout=10)
            preserved = (home / 'auth.json').read_text() if (home / 'auth.json').exists() else None
            return done, preserved

    def test_embedded_diagnostics_parse_real_js_escapes(self):
        script = r"""
const vm=require('vm');
let output='';
const fs={
  writeFileSync(){throw Object.assign(new Error(),{code:'EROFS'});},
  existsSync(){return false;},
  readFileSync(p){
    if(p.endsWith('status'))return 'CapEff:\t0000000000000000\n';
    if(p.endsWith('mountinfo'))return '1 0 0:1 / / rw - overlay overlay rw\n2 1 0:2 / /home/codex rw - tmpfs tmpfs rw\n3 1 0:3 / /tmp rw - tmpfs tmpfs rw\n4 1 0:4 / /work rw - tmpfs tmpfs rw';
    if(p.endsWith('cpu.max'))return '100000 100000';
    return p.endsWith('memory.max')?'1073741824':'128';
  }
};
const cp={spawnSync(c){return c==='codex'?{stdout:'codex-cli 0.166.0\n'}:{status:0,stdout:'{"mode":384,"valid":true}'};}};
const process={arch:'arm64',getuid:()=>10001,getgid:()=>10001,env:{CODEX_HOME:'/home/codex/.codex'},stdout:{write:x=>output=x}};
vm.runInNewContext(SOURCE,{require:n=>n==='fs'?fs:cp,process});
console.log(output);
""".replace('SOURCE', json.dumps(verify.DIAGNOSTIC))
        done = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), dict.fromkeys(verify.REQUIRED[2:-3], True))
        for field, mutated in (
                ('no_host_mounts', script.replace('/ /work rw - tmpfs tmpfs rw', '/ /work rw - virtiofs host rw')),
                ('limits', script.replace("return '100000 100000'", "return 'max 100000'"))):
            with self.subTest(field=field):
                done = subprocess.run(['node', '-e', mutated], capture_output=True, text=True, timeout=10)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertFalse(json.loads(done.stdout)[field])

    def test_exact_command_prompt_stdin_auth_mode_and_cleanup(self):
        probe = "const f=require('fs'),p=process.env.CODEX_HOME+'/auth.json';let x='';process.stdin.on('data',c=>x+=c);process.stdin.on('end',()=>console.log(JSON.stringify({mode:f.statSync(p).mode&511,auth:JSON.parse(f.readFileSync(p)).auth_mode,prompt:x==='synthetic prompt'})))"
        done, remaining = self.run_bootstrap(json.dumps({'auth': AUTH, 'prompt':'synthetic prompt'}), ['node', '-e', probe])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {'mode':384,'auth':'chatgpt','prompt':True})
        self.assertIsNone(remaining)

    def test_failures_hide_stderr_secrets_and_keep_existing_auth(self):
        envelope = json.dumps({'auth': AUTH, 'prompt':'synthetic prompt'})
        for command in ([], ['missing-iam-program'], ['node', '-e', "console.error('synthetic-secret');process.exit(3)"],
                        ['node', '-e', "process.kill(process.pid,'SIGTERM')"]):
            done, remaining = self.run_bootstrap(envelope, command)
            self.assertNotEqual(done.returncode, 0)
            self.assertNotIn('synthetic-secret', done.stderr + done.stdout)
            self.assertIsNone(remaining)
        done, remaining = self.run_bootstrap(envelope, ['node','-e',''], existing=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(remaining, 'preserve')

    def test_invalid_transport_rejected_without_secret_echo(self):
        payloads = ['not json', 'x' * (4 * 1024 * 1024 + 1)]
        invalid = [None, [], {}, {'auth':AUTH,'prompt':''}, {'auth':AUTH,'prompt':' '},
                   {'auth':AUTH,'prompt':'ok','command':['sh']}, {'auth':{},'prompt':'ok'},
                   {'auth':dict(AUTH, OPENAI_API_KEY='synthetic-secret'),'prompt':'ok'},
                   {'auth':dict(AUTH,auth_mode='api'),'prompt':'ok'}]
        for tokens in (None, [], {}, {'access_token': 'x'}, dict(AUTH['tokens'], account_id=''),
                       dict(AUTH['tokens'], account_id=1), dict(AUTH['tokens'], extra='x')):
            invalid.append({'auth':dict(AUTH,tokens=tokens),'prompt':'ok'})
        invalid.append({'auth':dict(AUTH,tokens=dict(AUTH['tokens'],account_id='account')),'prompt':5})
        payloads += [json.dumps(value) for value in invalid]
        for payload in payloads:
            done, remaining = self.run_bootstrap(payload, ['node', '-e', 'process.exit(0)'])
            self.assertNotEqual(done.returncode, 0)
            self.assertEqual(done.stderr, 'Codex bootstrap failed.\n')
            self.assertIsNone(remaining)
        done, _ = self.run_bootstrap(json.dumps({'auth':dict(AUTH,tokens=dict(AUTH['tokens'],account_id='account')),'prompt':'ok'}), ['node','-e','process.stdin.resume()'])
        self.assertEqual(done.returncode, 0)

    def test_private_handoff_after_success_or_failure_and_cleanup(self):
        auth = dict(AUTH, last_refresh='2026-10-30T10:00:00Z')
        probe = """
const fs=require('fs'),p=process.env.CODEX_HOME+'/auth.json';
process.stdin.resume();process.stdin.on('end',()=>{
 const auth=JSON.parse(fs.readFileSync(p));
 auth.tokens.refresh_token='synthetic-rotated';
 auth.tokens.account_id='synthetic-account';
 auth.tokens.login_owner_id='synthetic-owner';auth.tokens.oauth_provenance={source:'synthetic'};
 auth.OPENAI_API_KEY=null;auth.last_refresh='2026-10-30T11:00:00Z';
 fs.writeFileSync(p,JSON.stringify(auth));console.log('model-output');
 console.error('synthetic-stderr-secret');process.exit(STATUS);
});
"""
        for status in (0, 3):
            done, remaining = self.run_bootstrap(json.dumps({'auth':auth,'prompt':'synthetic prompt'}),
                                                 ['node','-e',probe.replace('STATUS', str(status))], writeback=True)
            self.assertEqual(done.returncode, status)
            lines = [line for line in done.stdout.splitlines() if line]
            self.assertEqual(lines[0], 'model-output')
            event = json.loads(lines[-1])
            self.assertEqual(set(event), {'type','auth'})
            self.assertEqual(event['type'], 'iam.auth.updated')
            self.assertEqual(event['auth']['tokens']['refresh_token'], 'synthetic-rotated')
            self.assertEqual(event['auth']['last_refresh'], '2026-10-30T11:00:00Z')
            self.assertNotIn('login_owner_id', event['auth']['tokens'])
            self.assertNotIn('oauth_provenance', event['auth']['tokens'])
            self.assertNotIn('synthetic-stderr-secret', done.stderr + done.stdout)
            self.assertIsNone(remaining)

    def test_invalid_private_handoff_never_echoes_file_or_partial_credentials(self):
        corruptions = ["fs.unlinkSync(p)", "fs.writeFileSync(p,'not JSON')", "fs.writeFileSync(p,'x'.repeat(65537))"]
        for changes in ("auth.OPENAI_API_KEY='synthetic-secret'", "auth.extra='synthetic-secret'",
                        "auth.tokens.extra='synthetic-secret'", "auth.auth_mode='api'", "auth.tokens=null"):
            corruptions.append("const auth=JSON.parse(fs.readFileSync(p));" + changes + ";fs.writeFileSync(p,JSON.stringify(auth))")
        for corrupt in corruptions:
            script = "const fs=require('fs'),p=process.env.CODEX_HOME+'/auth.json';process.stdin.resume();process.stdin.on('end',()=>{" + corrupt + "});"
            done, remaining = self.run_bootstrap(json.dumps({'auth':AUTH,'prompt':'synthetic prompt'}), ['node','-e',script], writeback=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertEqual(done.stdout, '')
            self.assertEqual(done.stderr, 'Codex auth handoff failed.\n')
            self.assertIsNone(remaining)
        for timestamp in (None, 'invalid'):
            done, remaining = self.run_bootstrap(json.dumps({'auth':dict(AUTH,last_refresh=timestamp),'prompt':'synthetic prompt'}),
                                                 ['node','-e',''], writeback=True)
            self.assertEqual(done.stderr, 'Codex bootstrap failed.\n')
            self.assertEqual(done.stdout, '')
            self.assertIsNone(remaining)


if __name__ == '__main__':
    unittest.main()
