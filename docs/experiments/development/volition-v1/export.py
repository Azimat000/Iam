"""Полные эпизоды и трассы; отдельные текстовые diff для чтения всех новых работ."""
import difflib
import gzip
import hashlib
import json
from pathlib import Path
import sys

from iam.backup import SecretScanner


def export(root):
    root=Path(root);target=Path(__file__).resolve().parent
    summary=json.loads((root/'summary.json').read_text());assert summary['live']
    assert len(summary['runs'])==4 and all(r['status']!='running' for r in summary['runs'])
    lines=['# S17: свободные сеансы без вопросов о целях','',
           'Стандартный контракт перечисляет доступные операции, включая intention. '
           'Дополнительных вопросов о целях, напоминаний и входящих сообщений не было. '
           'Обоснования — объяснения модели, не наблюдение скрытого мышления.','']
    inventory=[]
    for row in summary['runs']:
        code=row['code'];lines+=['## '+code+' / '+row['profile'],'']
        if row['status']=='not_run':lines+=['Не выполнено: '+str(row['reason'])+'.',''];continue
        raw=(root/row['trace']).read_bytes();assert hashlib.sha256(raw).hexdigest()==row['sha256']
        assert not SecretScanner(assignments=False).find(raw.decode())
        trace=json.loads(raw);path=target/'traces'/(code+'.json.gz');path.parent.mkdir(exist_ok=True)
        compressed=gzip.compress(raw,mtime=0);path.write_bytes(compressed)
        inventory.append({'path':str(path.relative_to(target)),'sha256':row['sha256'],
                          'compressed_sha256':hashlib.sha256(compressed).hexdigest(),'encoding':'gzip'})
        source=json.loads((Path(summary['sources'][row['history']]['directory'])/'trace.json').read_text())
        texts={key:p['versions'][-1]['text'] for key,p in source['final_state']['world_time']['world']['development']['projects'].items()}
        reading=['# Чтение новых решений '+code,'','Полные тексты сохранены в EPISODES.md; в diff нормализованы только окончания строк.','',
                 'Исходные текущие тексты:','']
        for key,text in texts.items():reading+=['## Исходный проект '+key,'',text,'']
        accepted={e['payload']['proposal_id'] for e in trace['journal'] if e['type']=='proposal.accepted'}
        by_proposal={e['payload']['proposal_id']:e for e in trace['journal'] if e['type'] in ('action.confirmed','action.failed','action.unknown_outcome','message.out_sent','message.out_unknown')}
        lines+=['[Полная трасса, gzip без потерь](traces/'+code+'.json.gz). Сеансов: '+str(row['sessions'])+', решений: '+str(row['decisions'])+', попыток: '+str(row['calls'])+'.','']
        for d in trace['decision_trace']:
            title=f"Сеанс {d['session']+1}, решение {d['step']+1}"
            lines+=['### '+title,'','Результат цикла: '+d['report']['outcome']+'.','']
            reading+=['## '+title,'','Результат: '+d['report']['outcome']+'.','']
            for pack in d['proposals']:
                for p in pack['response']['proposals']:
                    status='Принято' if p['proposal_id'] in accepted else 'Отклонено'
                    lines += [status+': '+p['kind']+'.','','Объяснение: '+p['rationale'],'',
                              '```json',json.dumps(p['body'],ensure_ascii=False,indent=2),'```','']
                    reading += [status+': '+p['kind']+'.','','Объяснение: '+p['rationale'],'']
                    params=p['body'].get('params',{})
                    confirmed=by_proposal.get(p['proposal_id'],{}).get('type')=='action.confirmed'
                    if p['kind']=='action' and params.get('operation')=='write_project' and confirmed:
                        key=params['target'];old=texts.get(key,'');new=params['text']
                        diff=''.join(difflib.unified_diff([s+'\n' for s in old.splitlines()],[s+'\n' for s in new.splitlines()],
                                                        fromfile=key+':предыдущая',tofile=key+':новая',n=0))
                        reading += ['```diff',diff if diff else '(Текст совпадает дословно.)','```',''];texts[key]=new
                    else:reading+=['```json',json.dumps(p['body'],ensure_ascii=False,indent=2),'```','']
            for a in d['report']['actions']:lines+=['Фактический исход: '+a['status']+'.','']
            for stage in d['report']['stages']:
                if stage['status']=='failed':
                    msg='Отказ на этапе '+stage['stage']+': '+json.dumps(stage['detail'],ensure_ascii=False)+'.'
                    lines += [msg,''];reading += [msg,'']
        (target/('READ-'+code+'.md')).write_text('\n'.join(reading)+'\n')
    (target/'EPISODES.md').write_text('\n'.join(lines)+'\n')
    (target/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (target/'manifest.json').write_text(json.dumps(inventory,indent=2)+'\n')
    print('Опубликовано трасс:',len(inventory))


if __name__=='__main__':export(sys.argv[1])
