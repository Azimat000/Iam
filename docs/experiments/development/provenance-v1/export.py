"""Публикация нормализованных трасс и всех новых эпизодов без SQLite/авторизации."""
import hashlib
import json
from pathlib import Path
import sys

from iam.backup import SecretScanner


def export(source):
    source=Path(source);target=Path(__file__).resolve().parent
    summary=json.loads((source/'summary.json').read_text());assert summary['live']
    lines=['# S17: проверка происхождения и свободные продолжения','',
           'Диагностические ответы получены после вопроса, не являются спонтанной речью или скрытым мышлением. '
           'Обоснования и тексты ниже сохранены полностью; технические ссылки внутри дословных ответов могут быть исходными.','']
    inventory=[]
    for row in summary['runs']:
        lines+=['## '+row['code'],'']
        if row['status']!='executed':lines+=['Не выполнено: '+row['reason']+'.',''];continue
        raw=(source/row['trace']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==row['sha256'] and not SecretScanner(assignments=False).find(raw.decode())
        trace=json.loads(raw);path=target/'traces'/(row['code']+'.json');path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
        inventory.append({'path':str(path.relative_to(target)),'sha256':row['sha256']})
        accepted={e['payload']['proposal_id'] for e in trace['journal'] if e['type']=='proposal.accepted'}
        lines += ['[Полная трасса](traces/'+row['code']+'.json). Вызовов: '+str(row['calls'])+'.','']
        for d in trace['decision_trace']:
            lines += ['### Решение '+str(d['step']+1),'','Результат цикла: '+d['report']['outcome']+'.','']
            for pack in d['proposals']:
                for p in pack['response']['proposals']:
                    lines += [('Принято: ' if p['proposal_id'] in accepted else 'Отклонено: ')+p['kind']+'.','',
                              'Объяснение модели: '+p['rationale'],'','```json',json.dumps(p['body'],ensure_ascii=False,indent=2),'```','']
            for action in d['report']['actions']:lines+=['Фактический исход: '+action['status']+'.','']
            for stage in d['report']['stages']:
                if stage['status']=='failed':lines+=['Техническая запись: '+json.dumps(stage['detail'],ensure_ascii=False)+'.','']
    (target/'EPISODES.md').write_text('\n'.join(lines)+'\n')
    (target/'manifest.json').write_text(json.dumps(inventory,indent=2)+'\n')
    (target/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print('Опубликовано трасс:',len(inventory))


if __name__=='__main__':export(sys.argv[1])
