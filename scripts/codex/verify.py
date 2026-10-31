#!/usr/bin/env python3
"""Repeatable isolation checks; reports contain only fixed names, statuses and safe metadata."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from iam.providers.codex import CodexProvider, CodexSandboxConfig, RESPONSE_JSON_SCHEMA, docker_run
from iam.providers.codex.auth import read_auth
from iam.providers.run import subprocess_runner

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ('schema', 'network_internal', 'runtime', 'non_root', 'read_only_root', 'no_capabilities',
            'no_host_mounts', 'no_socket', 'limits', 'ephemeral_auth', 'fresh_home',
            'net_allowed', 'net_denied', 'net_direct_denied')
DIAGNOSTIC = r"""
const fs=require('fs'),cp=require('child_process');
const out={};
out.non_root=process.getuid()===10001 && process.getgid()===10001;
try { fs.writeFileSync('/iam-root-probe','x'); out.read_only_root=false; }
catch(e) { out.read_only_root=e.code==='EROFS'; }
out.no_capabilities=/^CapEff:\s+0+$/m.test(fs.readFileSync('/proc/self/status','utf8'));
const mounts=fs.readFileSync('/proc/self/mountinfo','utf8').trim().split('\n').map(x=>({path:x.split(' ')[4],type:x.split(' - ')[1]?.split(' ')[0]}));
const allowed=['/','/tmp','/work','/home/codex','/etc/hosts','/etc/hostname','/etc/resolv.conf'];
out.no_host_mounts=mounts.every(x=>allowed.includes(x.path)||['/dev','/proc','/sys'].some(p=>x.path===p||x.path.startsWith(p+'/')))
  && ['/tmp','/work','/home/codex'].every(p=>mounts.some(x=>x.path===p&&x.type==='tmpfs'));
out.no_socket=!fs.existsSync('/var/run/docker.sock')&&!fs.existsSync('/run/docker.sock');
function bounded(p,max){try {const s=fs.readFileSync(p,'utf8').trim();return /^\d+$/.test(s)&&Number(s)>0&&Number(s)<=max;}catch{return false;}}
function cpuBounded(){try {const [q,p]=fs.readFileSync('/sys/fs/cgroup/cpu.max','utf8').trim().split(/\s+/).map(Number);return q>0&&p>0&&q/p<=1;}catch{return false;}}
out.limits=bounded('/sys/fs/cgroup/memory.max',1073741824)&&bounded('/sys/fs/cgroup/pids.max',128)&&cpuBounded();
out.runtime=process.arch==='arm64'&&cp.spawnSync('codex',['--version'],{encoding:'utf8'}).stdout.trim()==='codex-cli 0.166.0';
const home=process.env.CODEX_HOME;
out.fresh_home=!fs.existsSync(home+'/auth.json');
const auth={auth_mode:'chatgpt',tokens:{access_token:'synthetic-access',id_token:'synthetic-id',refresh_token:'synthetic-refresh'}};
const probe="const fs=require('fs');const p=process.env.CODEX_HOME+'/auth.json';process.stdin.resume();process.stdin.on('end',()=>process.stdout.write(JSON.stringify({mode:fs.statSync(p).mode&511,valid:JSON.parse(fs.readFileSync(p)).auth_mode==='chatgpt'})))";
const r=cp.spawnSync('node',['/opt/iam/launch.cjs','node','-e',probe],{input:JSON.stringify({auth,prompt:'synthetic prompt'}),encoding:'utf8'});
let check={};try{check=JSON.parse(r.stdout);}catch{}
out.ephemeral_auth=r.status===0&&check.mode===384&&check.valid===true&&!fs.existsSync(home+'/auth.json');
process.stdout.write(JSON.stringify(out));
"""
REQUEST = {'request_id': 'req_verify0001', 'persona_id': 'exp_verify0001', 'session_id': 'ses_verify0001',
           'epoch': 1, 'state_version': 0, 'purpose': 'appraise', 'issued_at': '2026-10-30T00:00:00.000Z',
           'life_time_ms': 0, 'cycle_id': 'cyc_verify0001', 'response_schema': 'response.schema.json',
           'idempotency_key': 'req:req_verify0001',
           'context': [{'ref': 'evt_verify0001', 'origin': 'observation', 'layer': 'notice',
                        'text': 'Isolation verification. Propose exactly one no_action with reason verification.'}],
           'allowed_proposals': ['no_action'], 'allowed_actions': [],
           'limits': {'max_output_tokens': 256, 'deadline_ms': 180000, 'attempt': 0}}


def verdict(results, live=False):
    required = (*REQUIRED, 'live') if live else REQUIRED
    return all(results.get(key) == 'PASS' for key in required) and 'FAIL' not in results.values()


def collect(cfg, *, live=False, runner=subprocess_runner, provider_cls=CodexProvider):
    results = dict.fromkeys(REQUIRED, 'UNVERIFIED')
    results['live'] = 'UNVERIFIED'

    def record(key, passed):
        results[key] = 'PASS' if passed else 'FAIL'

    def diagnostic(command):
        cmd = docker_run(cfg, command=command)
        return runner(cmd.argv, '', 60, cmd.kill_argv())

    try:
        schema = json.loads((ROOT / 'docker/response.schema.json').read_text())
        record('schema', schema == RESPONSE_JSON_SCHEMA)
    except (OSError, ValueError):
        record('schema', False)
    network = runner(('docker', 'network', 'inspect', cfg.network), '', 30, None)
    try:
        record('network_internal', network.returncode == 0 and json.loads(network.stdout)[0]['Internal'] is True)
    except (ValueError, KeyError, IndexError, TypeError):
        record('network_internal', False)
    runtime = diagnostic(('node', '-e', DIAGNOSTIC))
    try:
        data = json.loads(runtime.stdout) if runtime.returncode == 0 else {}
    except ValueError:
        data = {}
    for key in REQUIRED[2:-3]:
        record(key, isinstance(data, dict) and data.get(key) is True)
    # A second container must start with no auth file, even after bootstrap ran in the first.
    fresh = diagnostic(('node', '-e', "process.exit(require('fs').existsSync(process.env.CODEX_HOME+'/auth.json')?1:0)"))
    record('fresh_home', results['fresh_home'] == 'PASS' and fresh.returncode == 0)
    for key, url, direct in (('net_allowed', 'https://chatgpt.com', False),
                             ('net_denied', 'https://example.com', False),
                             ('net_direct_denied', 'https://chatgpt.com', True)):
        args = ['curl', '--silent', '--output', '/dev/null', '--connect-timeout', '8', '--max-time', '15']
        args += ['--noproxy', '*'] if direct else ['--proxy', cfg.proxy_url]
        response = diagnostic((*args, '--write-out', '%{http_connect}', url))
        if direct:
            passed = response.returncode in (5, 6, 7, 28) and not response.timed_out
        else:
            passed = response.stdout.strip() == ('200' if key == 'net_allowed' else '403')
            passed = passed and response.returncode == (0 if key == 'net_allowed' else 56)
        record(key, passed)
    if live:
        # Never spend a subscription request when isolation has already failed.
        if verdict(results):
            try:
                response = provider_cls(runner, cfg, max_format_retries=0).propose(REQUEST)
                proposals = response['proposals']
                record('live', response['provider']['model'] == 'gpt-6-luna'
                       and response['tool_use_detected'] is False and len(proposals) == 1
                       and proposals[0]['kind'] == 'no_action')
            except Exception:
                record('live', False)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--auth-file')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--persist-auth-updates', action='store_true')
    args = parser.parse_args(argv)
    try:
        output = Path(args.output_dir)
        if not output.is_absolute() or '..' in output.parts or output.resolve().is_relative_to(ROOT):
            raise ValueError
        report_path = output / 'report.json'
        if report_path.is_symlink() or (args.auth_file and report_path.resolve() == Path(args.auth_file).resolve()):
            raise ValueError
        if args.auth_file and report_path.exists() and report_path.samefile(args.auth_file):
            raise ValueError
        if args.live and not args.auth_file:
            raise ValueError
        if args.persist_auth_updates and not args.live:
            raise ValueError
        cfg = CodexSandboxConfig(auth_file=args.auth_file, persist_auth_updates=args.persist_auth_updates)
        if args.auth_file:
            read_auth(args.auth_file, ROOT)
        output.mkdir(parents=True, exist_ok=True)
    except Exception:
        print('Unsafe or unavailable input: use an external output directory and subscription auth file with mode 600.', file=sys.stderr)
        return 2
    results = collect(cfg, live=args.live)
    passed = verdict(results, args.live)
    report = {'model': 'gpt-6-luna', 'cli_version': '0.166.0', 'live_requested': args.live,
              'auth_updates_enabled': args.persist_auth_updates,
              'isolation_passed': verdict({key: results[key] for key in REQUIRED}), 'passed': passed, 'checks': results}
    try:
        fd, temporary = tempfile.mkstemp(prefix='.report-', dir=output)
        os.close(fd)
        try:
            Path(temporary).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            os.replace(temporary, report_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError:
        print('Could not write verification report.', file=sys.stderr)
        return 2
    print('PASS' if passed else 'FAIL')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
