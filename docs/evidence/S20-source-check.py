"""Сверка неизменного кода полного прогона S20; метаданные требований проверяются отдельно."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else '/Users/azamatsibgatullin/.iam-evidence/s20-attention-20261101/workspace')
expected = json.loads((ROOT / 'docs/evidence/S20-source-hashes.json').read_text())
checked = {}
for rel, wanted in expected['files'].items():
    tested = hashlib.sha256((TARGET / rel).read_bytes()).hexdigest()
    assert tested == wanted, 'Изменилась проверочная копия: ' + rel
    if rel != 'requirements.toml':
        actual = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        assert actual == wanted, 'Изменился код после полного прогона: ' + rel
        checked[rel] = actual
base_state = json.loads(subprocess.check_output(['git', 'show', expected['base'] + ':docs/state.json'], cwd=ROOT))
current_state = json.loads((ROOT / 'docs/state.json').read_text())
assert base_state['gates'] == current_state['gates'], 'Изменены барьеры'
for rel in ('docs/PLAN.md', 'docs/COMPLETION.md'):
    assert (ROOT / rel).read_bytes() == subprocess.check_output(['git', 'show', expected['base'] + ':' + rel], cwd=ROOT), rel
report = {'base': expected['base'], 'attempt': expected['attempt'], 'tested_files': len(expected['files']),
          'unchanged_code_files': len(checked), 'code_sha256': checked,
          'requirement_metadata_sha256': hashlib.sha256((ROOT / 'requirements.toml').read_bytes()).hexdigest(),
          'gates_unchanged': True, 'plan_and_completion_unchanged': True}
(ROOT / 'docs/evidence/S20-final-source-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print('PASS:', len(checked), 'неизменных кодовых файлов;', len(expected['files']), 'файлов точной проверочной копии; барьеры/PLAN/COMPLETION сохранены')
