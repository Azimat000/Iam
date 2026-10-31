"""D049: только предоставленные материалы; ни будущих целей, ни живых решений."""
import hashlib
import json
from pathlib import Path
import sys

from iam.domain import new_id
from iam.experiments import Persona
from iam.experiments.development import CAP_DEVELOPMENT, DevelopmentRuntime
from iam.experiments.provenance import seed_history
from iam.experiments.report.development import save_trace
from iam.experiments.transfer import TransferArchitecture
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, act

ROOT=Path(__file__).resolve().parents[4]
SWITCHES={'memory_off':False,'motivation_off':False,'interests_off':False,'relationships_off':False}
CONTEXTS={
 'fiction':('creative_work','На площадке','На площадке лежал мокрый след. Дверь напротив была приоткрыта. Внизу щёлкнул замок, но шагов не было слышно.'),
 'numbers':('investigation','Числовые наблюдения','Предоставленная таблица: n=1,2,3,4,5; y=1,3,6,10,15. Во второй записи: n=1,2,3; y=1,4,9. Других измерений в таблице нет.'),
 'explanation':('understanding_people','Две реплики','Предоставлены две реплики о тексте. Первая: «Мне понравился рассказ, хотя я прочитал только начало». Вторая: «В третьем абзаце непонятно, кто говорит». Сведений о том, кто прав и как автор воспринял реплики, нет.'),
}


def prepare(output):
 output=Path(output);output.mkdir(parents=True,exist_ok=False);persona=Persona.load(ROOT/'scenarios/personas/curious.toml');rows=[]
 for code,(topic,title,text) in CONTEXTS.items():
  directory=output/code;directory.mkdir()
  with Store.create(directory/'persona.db',new_id('exp')) as store:
   r=DevelopmentRuntime(store,persona,SWITCHES);r.step({'kind':'session','action':'start'})
   p=ScriptedProvider();a=TransferArchitecture(lambda _:p);cycle=a.build(r)
   p.queue(lambda q:Reply(proposals=(act(CAP_DEVELOPMENT,{'operation':'write_project','target':'draft','topic':topic,'title':title,'text':text},cites=[c['ref'] for c in q['context']]),)))
   r.step({'kind':'wait','life_ms':60000});report=cycle.run(r.trigger('provided_material'))
   assert report.actions[0].status=='confirmed';cutoff=store.last_seq();r.step({'kind':'session','action':'stop'})
  raw,errors=save_trace(directory,a,persona,[],[],SWITCHES);assert not errors
  seed=directory/'seed-trace.json';seed.write_bytes(raw.trace_bytes)
  with Store.open(directory/'persona.db') as store:
   r=DevelopmentRuntime(store,persona,SWITCHES);r.step({'kind':'session','action':'start'})
   seed_history(r,seed,hashlib.sha256(raw.trace_bytes).hexdigest(),cutoff);r.step({'kind':'session','action':'stop'})
  final,errors=save_trace(directory,a,persona,[],[],SWITCHES);assert not errors
  rows.append({'code':code,'status':'prepared','trace':code+'/trace.json','sha256':hashlib.sha256(final.trace_bytes).hexdigest(),'setup_seq':cutoff})
 (output/'summary.json').write_text(json.dumps({'protocol':'commitment-sources-v1','runs':rows,'goals_seeded':0,'live_calls':0},ensure_ascii=False,indent=2)+'\n')
 return rows


if __name__=='__main__':prepare(sys.argv[1])
