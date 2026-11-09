"""Одно техническое непустое attention, без личности и без применения к ядру."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from iam.domain import jsonschema, new_id
from iam.providers import CodexProvider, CodexSandboxConfig, subprocess_runner
from iam.providers.codex.auth import read_auth
from iam.providers.codex.classify import _json
from iam.providers.codex.command import REPO_ROOT, RESPONSE_JSON_SCHEMA
from integration.answer_audit import record_answer
from integration import live_series as series

TARGET = 'inp_FixtureNotice01'


def verify_attention(output, auth_file, image, *, runner=subprocess_runner):
    output = Path(output).resolve()
    if output.exists(): raise ValueError('A new fixture output is required')
    cfg = CodexSandboxConfig(auth_file=auth_file, image=image, persist_auth_updates=True)
    auth = read_auth(auth_file, REPO_ROOT)
    output.mkdir(mode=0o700)
    artifacts = series.audit.Artifacts(output, private_paths=[auth_file])
    rid = new_id('req')
    request = {'request_id':rid,'persona_id':new_id('exp'),'session_id':new_id('ses'),
        'epoch':1,'state_version':0,'issued_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
        'life_time_ms':0,'purpose':'choose_action','allowed_proposals':['attend'],'allowed_actions':[],
        'context':[{'ref':new_id('evt'),'origin':'model_knowledge','layer':'state','text':
        'Это техническая проверка формата, не выбор личности. Верни ровно одно непустое attention '
        'с focus=dialogue, ref=inp_FixtureNotice01, cites=[] и кратким rationale; proposals=[]. '
        'Это вымышленный идентификатор тестового уведомления. Никакое чтение или изменение состояния '
        'не будет выполнено; цель и добровольность здесь не проверяются.'}],
        'limits':{'max_output_tokens':400,'deadline_ms':60000,'attempt':0},
        'response_schema':'response.schema.json','idempotency_key':'req:'+rid,'cycle_id':new_id('cyc')}
    assert not jsonschema.errors('request', request)
    artifacts.write('manifest.json', {'scope':'live transport fixture, not persona behavior; no core application',
        'image':image,'schema':RESPONSE_JSON_SCHEMA,'source_sha256':series.sha(Path(__file__)),
        'max_physical_calls':1,'persona_decisions':0,'expected_target':TARGET}, exclusive=True)
    calls = 0
    native_nonnull = False
    def transport(argv, stdin, timeout, cleanup=None):
        nonlocal calls, native_nonnull
        if calls: raise AssertionError('No fixture retries')
        calls += 1
        artifacts.write('attempt.json', {'index':calls,'status':'reserved'}, exclusive=True)
        result = runner(argv, stdin, timeout, cleanup)
        physical = record_answer(artifacts, 'fixture', calls, result, auth=auth)
        try:
            data = _json(physical['messages'][0]) if physical['body_available'] else None
            native_nonnull = (not physical['redactions'] and not jsonschema.check(RESPONSE_JSON_SCHEMA,data,Path('.'))
                              and isinstance(data.get('attention'),dict)
                              and data['attention']['focus']=='dialogue' and data['attention']['ref']==TARGET
                              and data['attention']['cites']==[])
        except (ValueError,RecursionError):
            native_nonnull = False
        return result
    provider = CodexProvider(transport,cfg,model=series.MODEL,max_format_retries=0,timeout_s=60)
    result = {'status':'failed','model_calls':0,'persona_decisions':0,'core_applied':False}
    try:
        response = provider.propose(request)
        artifacts.write('response.json',response)
        proposals=response['proposals']
        expected=(len(proposals)==1 and proposals[0]['kind']=='attend'
                  and proposals[0]['body']=={'focus':'dialogue','ref':TARGET} and proposals[0]['cites']==[])
        result.update(status='passed' if expected and native_nonnull else 'declined_or_wrong_shape',
                      shared_valid=not jsonschema.errors('response',response))
    except Exception as exc:
        result.update(error_class=type(exc).__name__)
    result['model_calls']=calls
    result['native_nonnull_wire3']=native_nonnull
    artifacts.write('result.json',result)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--auth-file',required=True)
    parser.add_argument('--image',required=True)
    args=parser.parse_args()
    result=verify_attention(args.output,args.auth_file,args.image)
    print(json.dumps(result))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__': raise SystemExit(main())
