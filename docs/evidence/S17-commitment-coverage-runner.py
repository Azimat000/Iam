"""Measure CLI tools explicitly when automatic subprocess coverage misses them on this Mac."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path.cwd().resolve()
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
CLI_TOOLS = {str(ROOT / 'tools' / name) for name in ('memory.py', 'reqs.py', 'covcheck.py')}
original_run = subprocess.run
traced_calls = 0

def measured_run(args, *positional, **kwargs):
    global traced_calls
    if isinstance(args, (list, tuple)) and len(args) >= 2 and str(args[1]) in CLI_TOOLS:
        args = [args[0], '-m', 'coverage', 'run', '-p', '--data-file=/Users/azamatsibgatullin/.iam-evidence/s17-commitment-20261031/checks/verified-cli', '--rcfile=' + str(ROOT / 'pyproject.toml'), *args[1:]]
        env = dict(kwargs.get('env') or os.environ)
        kwargs['env'] = {k: v for k, v in env.items() if not k.startswith('COVERAGE_PROCESS_')}
        traced_calls += 1
    return original_run(args, *positional, **kwargs)

subprocess.run = measured_run
suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
result = unittest.TextTestRunner(verbosity=1).run(suite)
print('CLI calls explicitly measured:', traced_calls)
sys.exit(not result.wasSuccessful())
