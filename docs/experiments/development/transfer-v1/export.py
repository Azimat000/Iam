"""Полные тексты коротких эпизодов и исходные нормализованные трассы без SQLite/входа.

PYTHONPATH=src .venv/bin/python docs/experiments/development/transfer-v1/export.py /исходная/серия
Запускать после verify.py. Публикует данные рядом с этим скриптом.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

from iam.backup import SecretScanner


def export(source):
    root=Path(source)
    target=Path(__file__).resolve().parent
    summary=json.loads((root/'summary.json').read_text())
    assert summary['live'], 'заглушечные данные не являются живой серией'
    lines=['# S17: все короткие эпизоды D-040','',
           'Подготовленное прошлое создано заглушкой. Ниже — выбор живой модели после него. '
           'Обоснования являются объяснениями модели, а не наблюдением её скрытого мышления. '
           'Отказы и таймауты сохранены; численных оценок пользователя нет.','']
    inventory=[]
    for row in summary['runs']:
        code=f"{row['scene']}-{row['mode']}-{row['repeat']}"
        lines+=['## '+code,'']
        if row['status']!='executed':
            lines+=['Не выполнено: '+row.get('reason','неизвестно')+'.','']
            continue
        raw=(root/row['trace']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==row['sha256']
        assert not SecretScanner(assignments=False).find(raw.decode())
        trace=json.loads(raw)
        path=target/'traces'/(code+'.json')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        inventory.append({'path':str(path.relative_to(target)),'sha256':row['sha256']})
        journal=trace['journal']
        accepted={e['payload']['proposal_id'] for e in journal if e['type']=='proposal.accepted'}
        lines += [f"[Полная трасса](traces/{code}.json). Вызовов: {row['calls']}; исходы действий: {row['action_outcomes']}.",'']
        for choice in trace['decision_trace']:
            lines += ['### Выбор '+str(choice['step']+1),'',
                      'Результат цикла: '+choice['report']['outcome']+'.','']
            signal=json.loads(choice['requests'][0]['context'][-1]['text'])['motivation']
            lines += ['Мотивация: '+('выключена.' if not signal['enabled'] else signal['mechanism']+'.'),'']
            for item in signal.get('items',[]):
                lines += [f"- {item['activity']}: сигнал {item['salience']:.3f}; "
                          f"повтор текста {item['last_two_visible_texts_identical']}; "
                          f"неудач подряд {item['inputs']['failures']}; принятые цели {item['accepted_goals']}."]
            lines.append('')
            for pack in choice['proposals']:
                response=pack['response']
                for proposal in response['proposals']:
                    lines += [('Принято: ' if proposal['proposal_id'] in accepted else 'Отклонено: ')+proposal['kind']+'.','',
                              'Обоснование модели: '+proposal['rationale'],'',
                              '```json',json.dumps(proposal['body'],ensure_ascii=False,indent=2),'```','']
            for action in choice['report']['actions']:
                lines += ['Исход действия: '+action['status']+'.','']
            for stage in choice['report']['stages']:
                if stage['status']=='failed':
                    lines += ['Техническая запись: '+json.dumps(stage['detail'],ensure_ascii=False)+'.','']
    (target/'EPISODES.md').write_text('\n'.join(lines)+'\n')
    (target/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (target/'manifest.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2)+'\n')
    return len(inventory)


if __name__=='__main__':
    print('Экспортировано трасс:',export(sys.argv[1]))
