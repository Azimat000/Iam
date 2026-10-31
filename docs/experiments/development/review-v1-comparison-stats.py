from pathlib import Path
from collections import Counter
import json,sys
root=Path(sys.argv[1]);summary=json.loads((root/'summary.json').read_text())
bycell=[]
for row in summary['runs']:
 t=json.loads((root/row['cell']/'trace.json').read_text());state=t['final_state'];events=t['journal']
 projects=state['world_time']['world']['development']['projects'];goal=state['intentions']
 requests=[e['payload']['request'] for e in events if e['type']=='proposal.requested']
 proposals={p['proposal_id']:p for e in events if e['type']=='proposal.received' for p in e['payload']['response']['proposals']}
 operations=Counter(proposals[e['payload']['proposal_id']]['body']['params']['operation'] for e in events if e['type']=='action.confirmed')
 hypotheses=Counter(m['content']['domain'] for m in t['memory_records'] if m['origin']=='own_hypothesis')
 retries=sum(bool(r.get('retry_of')) for r in requests)
 failed_appraise=[d['step'] for d in t['decision_trace'] for s in d['report']['stages']
  if s['detail'].get('phase')=='development_appraisal' and s['detail']['report']['outcome']!='done']
 bycell.append({'cell':row['cell'],'variant':row['variant'],'history':row['history'],'repeat':row['repeat'],
 'calls_main_and_probes':row['calls'],'choices':len(t['decision_trace']),'done':row['done'],
 'main_requests_by_purpose':dict(Counter(r['purpose'] for r in requests)),
 'main_format_retries':retries,'failed_appraisal_stages':len(failed_appraise),
 'projects':len(projects),'versions':sum(len(p['versions']) for p in projects.values()),
 'projects_across_sessions':sum(len({v['session_id'] for v in p['versions']})>1 for p in projects.values()),
 'recorded_hypotheses':dict(hypotheses),'intentions':len(goal),'intentions_done':sum(g['status']=='done' for g in goal.values()),
 'sent_messages':sum(e['type']=='message.out_sent' for e in events),'confirmed_operations':dict(operations),
 'rejected_proposals':sum(e['type']=='proposal.rejected' for e in events)})
variants=[]
for variant in 'ABC':
 rows=[r for r in bycell if r['variant']==variant]; result={'variant':variant,'biographies':len(rows)}
 for field in ('calls_main_and_probes','choices','done','main_format_retries','failed_appraisal_stages','projects','versions','projects_across_sessions','intentions','intentions_done','sent_messages','rejected_proposals'):
  result[field]=sum(r[field] for r in rows)
 for field in ('main_requests_by_purpose','recorded_hypotheses','confirmed_operations'):
  counts=Counter()
  for r in rows:counts.update(r[field])
  result[field]=dict(counts)
 variants.append(result)
print(json.dumps({'model':'gpt-6-luna','personality_scores':None,'variants':variants,'biographies':bycell},ensure_ascii=False,indent=2))
