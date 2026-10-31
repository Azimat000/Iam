"""Независимая сверка полного читаемого пакета с исходным журналом."""
from pathlib import Path
import csv, hashlib, json, sys
raw, published = map(Path, sys.argv[1:3])
manifest=json.loads((raw/'series-manifest.json').read_text())
repo=Path.cwd()
for name, sha in manifest['execution_sources'].items():
    assert hashlib.sha256((repo/name).read_bytes()).hexdigest()==sha, name
keypath=Path(sys.argv[3]) if len(sys.argv)>3 else raw/'private/fallible-v1-key.json'
assert not keypath.resolve().is_relative_to(repo)
key=json.loads(keypath.read_text()); assert len(key['items'])==12 and not key['omitted']
blind=published/'blind'
expected={'INDEX.md','README.md','ratings.csv'}|{item['id']+'.md' for item in key['items']}
assert {p.name for p in blind.iterdir()}==expected
rows=list(csv.DictReader((blind/'ratings.csv').open()))
assert len(rows)==12 and all(not row[field] for row in rows for field in row if field!='id')
checks=[]
for item in key['items']:
    tracepath=Path(item['trace']); t=json.loads(tracepath.read_text())
    packet=(blind/(item['id']+'.md')).read_text()
    assert hashlib.sha256(packet.encode()).hexdigest()==item['sha256']
    assert packet.count('\n## Сеанс ')==6
    assert packet.count('\n### Момент выбора ')==18
    proposals={p['proposal_id']:p for d in t['decision_trace'] for r in d['proposals'] for p in r['response']['proposals']}
    memory={r['memory_id']:r for r in t['memory_records']}
    arrived={e['payload']['input_id']:e['payload']['text'] for e in t['journal'] if e['type']=='input.arrived'}
    fragments=[]; nworks=nsent=nread=nhyp=0
    for e in t['journal']:
        p=e['payload']; kind=e['type']
        if kind=='action.confirmed':
            proposal=proposals[p['proposal_id']]; params=proposal['body']['params']
            if params['operation']=='write_project':
                fragments.append(params['text']); nworks+=1
            elif params['operation']=='read_material': fragments.append(p['summary'])
        elif kind=='message.out_sent': fragments.append(proposals[p['proposal_id']]['body']['text']); nsent+=1
        elif kind=='input.read': fragments.append(arrived[p['input_id']]); nread+=1
        # Смотрим добавленные записи в переходе состояния, независимо от имени события.
        for change in p.get('changes', []):
            record=change.get('after')
            if (change['group']=='memory' and change['op']=='add' and record
                    and record.get('origin')=='own_hypothesis'):
                label='пересмотр собственного предположения' if any(link['type']=='supersedes' for link in record.get('links', [])) else 'собственное предположение'
                fragments.append('**Внутренняя запись — '+label+':**\n\n'+record['content']['text']); nhyp+=1
    assert nhyp==sum(m['origin']=='own_hypothesis' for m in t['memory_records'])
    assert packet.count('**Внутренняя запись — ')==nhyp
    position=0
    for text in fragments:
        found=packet.find(text, position)
        assert found>=0, (item['id'], 'пропущен/изменён/переставлен исходный текст')
        position=found+len(text)
    # В начале каждого сегмента до фактического чтения текст уведомления не раскрыт.
    read_positions=[packet.index('**Прочитано от человека:**\n\n'+arrived[e['payload']['input_id']])
                    for e in t['journal'] if e['type']=='input.read']
    for e, pos in zip([e for e in t['journal'] if e['type']=='input.read'],read_positions):
        assert arrived[e['payload']['input_id']] not in packet[:pos]
    checks.append({'id':item['id'],'sessions':6,'choices':18,'original_work_versions':nworks,
                   'sent_messages':nsent,'read_messages':nread,'accepted_hypotheses':nhyp,'status':'pass'})
assert {row['id'] for row in rows}=={row['id'] for row in checks}
print(json.dumps({'policy':key['policy'],'biographies':len(checks),'source_hashes_unchanged':True,
 'private_key_outside_repository':True,'ratings_empty':True,'original_texts_ordered_and_unchanged':True,
 'checks':checks},ensure_ascii=False,indent=2))
