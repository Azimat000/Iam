"""Сверить проверочную копию с Git-базой и незакоммиченными файлами S18.

Исторические dataless-артефакты не обходятся. Неизменённый код сверяется с Git,
а все 230 исходных файлов дополнительно читаются с диска и сравниваются с копией.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path.cwd()
TARGET = Path('/Users/azamatsibgatullin/.iam-evidence/s18-memory-20261101/workspace')
def git(*args):
    return subprocess.check_output(['git', '-c', 'diff.autoRefreshIndex=false', *args], cwd=ROOT)
SCOPES = ['src/iam/memory', 'src/iam/state/controller.py', 'src/iam/cycle/cycle.py',
          'src/iam/interfaces/protocols.py', 'src/iam/panel', 'tests/memory',
          'tests/panel/test_memory.py', 'pyproject.toml', 'requirements.toml',
          'docs/DECISIONS.md', 'docs/JOURNAL.md', 'docs/STATE.md', 'docs/state.json',
          'docs/slices/S18-memory-1.md', 'docs/evidence/S18-*']
changed = set(git('diff', '--name-only', 'HEAD', '--', *SCOPES).decode().splitlines())
new = {p for p in git('ls-files', '--others', '--exclude-standard').decode().splitlines()
       if Path(p).name != '.DS_Store'}
for rel in sorted(changed | new):
    source = ROOT / rel
    if source.is_file():
        (TARGET / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, TARGET / rel)
tracked = git('ls-files', 'src', 'tests', 'tools', 'pyproject.toml').decode().splitlines()
paths = sorted(set(tracked) | {p for p in new if p.startswith(('src/', 'tests/', 'tools/'))})
result = {}
for rel in paths:
    data = (ROOT / rel).read_bytes() if rel in changed | new else git('show', 'HEAD:' + rel)
    actual = (TARGET / rel).read_bytes()
    assert (ROOT / rel).read_bytes() == data, rel + ": unexpected working-tree change"
    assert actual == data, rel
    result[rel] = hashlib.sha256(data).hexdigest()
report = {'base': git('rev-parse', 'HEAD').decode().strip(), 'verified_files': len(result),
          'method': 'Git baseline for unchanged code; every source/test/tool/config independently read from disk and matched to copy',
          'files': result}
(ROOT / 'docs/evidence/S18-source-hashes.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print('Совпадают файлов:', len(result))
