"""Байтовая сверка проверочной копии с рабочими исходниками S19; без обхода dataless-архивов."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
TARGET = Path('/Users/azamatsibgatullin/.iam-evidence/s19-memory-20261101/workspace')
SCOPES = ['src/iam/memory', 'src/iam/state/controller.py', 'src/iam/cycle/cycle.py',
          'src/iam/panel/app.py', 'src/iam/panel/runtime.py', 'src/iam/panel/views.py', 'src/iam/panel/memory.py',
          'tests/memory', 'tests/panel/test_memory.py', 'requirements.toml', 'docs/DECISIONS.md',
          'docs/JOURNAL.md', 'docs/STATE.md', 'docs/state.json', 'docs/slices/S19-memory-2.md', 'docs/evidence/S19-*']
def git(*args):
    return subprocess.check_output(['git', '-c', 'diff.autoRefreshIndex=false', *args], cwd=ROOT)
changed = set(git('diff', '--name-only', 'HEAD', '--', *SCOPES).decode().splitlines())
new = {'src/iam/memory/lifecycle.py', 'tests/memory/test_lifecycle.py'}
for rel in sorted(changed | new):
    target = TARGET / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / rel, target)
paths = set(git('ls-files', 'src', 'tests', 'tools', 'pyproject.toml', 'requirements.toml').decode().splitlines()) | new
hashes = {}
for rel in sorted(paths):
    data = (ROOT / rel).read_bytes()
    if rel not in changed | new:
        assert data == git('show', 'HEAD:' + rel), 'Unexpected pre-existing change: ' + rel
    assert data == (TARGET / rel).read_bytes(), rel
    hashes[rel] = hashlib.sha256(data).hexdigest()
report = {'base': git('rev-parse','HEAD').decode().strip(), 'verified_files':len(hashes), 'files':hashes}
(ROOT / 'docs/evidence/S19-source-hashes.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print('Совпадают файлов:', len(hashes))
