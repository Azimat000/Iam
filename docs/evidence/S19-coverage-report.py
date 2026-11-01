"""Объединить только законченный прогон S19, явно включая части CLI и основного процесса."""
from pathlib import Path
import re
import subprocess
import sys

folder = Path(sys.argv[1]).resolve()
log = (folder / 'tests.txt').read_text()
if not re.search(r'Ran 1155 tests in [\d.]+s\n\nOK\n', log):
    raise SystemExit('Полный прогон не прошёл; успешная приёмка/объединение запрещены')
parts = sorted(p for p in folder.iterdir() if p.name.startswith(('cli.', 'main.')))
assert parts and all(p.read_bytes()[:16] == b'SQLite format 3\x00' for p in parts)
with (folder / 'combine.txt').open('w') as out:
    subprocess.run([sys.executable, '-m', 'coverage', 'combine', '--keep',
                    '--data-file=' + str(folder / 'combined'), *map(str, parts)], stdout=out, stderr=subprocess.STDOUT, check=True)
subprocess.run([sys.executable, '-m', 'coverage', 'json', '--data-file=' + str(folder / 'combined'),
                '-o', str(folder / 'coverage.json')], check=True)
with (folder / 'coverage.txt').open('w') as out:
    subprocess.run([sys.executable, 'tools/covcheck.py', '--json', str(folder / 'coverage.json')],
                    stdout=out, stderr=subprocess.STDOUT, check=True)
print('Частей текущего прогона:', len(parts))
print((folder / 'coverage.txt').read_text().splitlines()[-1])
