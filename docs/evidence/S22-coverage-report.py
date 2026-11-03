"""Отчёт только для завершённого успешного полного прогона; каталог указывается первым аргументом."""
from pathlib import Path
import re,subprocess,sys
folder=Path(sys.argv[1]).resolve()
log=(folder/'tests.txt').read_text()
m=re.search(r'Ran (\d+) tests in ([\d.]+)s\n\nOK\n',log)
assert m,'Полный прогон не закончен успешно'
assert 'skipped=' not in log
parts=sorted(p for p in folder.iterdir() if p.name.startswith(('cli.','main.')))
assert parts
subprocess.run([sys.executable,'-m','coverage','combine','--keep','--data-file='+str(folder/'combined'),*map(str,parts)],stdout=(folder/'combine.txt').open('w'),check=True)
subprocess.run([sys.executable,'-m','coverage','json','--data-file='+str(folder/'combined'),'-o',str(folder/'coverage.json')],check=True)
with (folder/'coverage.txt').open('w') as out:
 result=subprocess.run([sys.executable,'tools/covcheck.py','--json',str(folder/'coverage.json')],stdout=out,stderr=subprocess.STDOUT)
print('Tests:',m.group(1),'seconds:',m.group(2),'coverage parts:',len(parts),'covcheck exit:',result.returncode)
print((folder/'coverage.txt').read_text())
sys.exit(result.returncode)
