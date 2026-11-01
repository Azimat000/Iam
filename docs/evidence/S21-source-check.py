"""Сверка неизменного кода полного прогона S21; метаданные требований отдельно."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else '/Users/azamatsibgatullin/.iam-evidence/s21-emotion-20261101/workspace')
expected = json.loads((ROOT / 'docs/evidence/S21-source-hashes.json').read_text())

def check(item):
    rel, wanted = item
    assert hashlib.sha256((TARGET / rel).read_bytes()).hexdigest() == wanted, 'Изменилась проверочная копия: ' + rel
    if rel != 'requirements.toml':
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == wanted, 'Изменился код после полного прогона: ' + rel
        return rel, wanted
    return None

with ThreadPoolExecutor(max_workers=8) as pool:
    checked = dict(result for result in pool.map(check, expected['files'].items()) if result is not None)
base_state = json.loads(subprocess.check_output(['git', 'show', expected['base'] + ':docs/state.json'], cwd=ROOT))
current_state = json.loads((ROOT / 'docs/state.json').read_text())
assert base_state['gates'] == current_state['gates'], 'Изменены барьеры'
for rel in ('docs/PLAN.md', 'docs/COMPLETION.md'):
    assert (ROOT / rel).read_bytes() == subprocess.check_output(['git', 'show', expected['base'] + ':' + rel], cwd=ROOT), rel
report = {'base': expected['base'], 'attempt': expected['attempt'], 'tested_files': len(expected['files']),
          'unchanged_code_files': len(checked), 'code_sha256': checked,
          'requirement_metadata_sha256': hashlib.sha256((ROOT / 'requirements.toml').read_bytes()).hexdigest(),
          'gates_unchanged': True, 'plan_and_completion_unchanged': True}
(ROOT / 'docs/evidence/S21-final-source-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print('PASS:', len(checked), 'неизменных кодовых файлов;', len(expected['files']), 'файлов точной проверочной копии; барьеры/PLAN/COMPLETION сохранены')
