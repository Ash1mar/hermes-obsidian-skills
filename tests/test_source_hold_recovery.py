"""Isolated authenticated source hold recovery; never real semantic acceptance."""
import copy
import hashlib
import json
import sqlite3

import pytest

from test_p3_knowledge_build import vault
from test_reduce_execution_recovery import stopped_reducer
from hermes_source_units import ContractError, mutation_digest
from hermes_source_units.validation import fingerprint
import fixed_reduce
import fixed_reduce_packets
import fixed_semantic
from fixed_semantic_schema import schema
import source_hold_recovery as recovery


@pytest.mark.parametrize('change',['valid','narrowed','audit','message','evidence','binding','live'])
def test_reviewed_source_hold_requires_exact_current_native_evidence(vault,monkeypatch,tmp_path,change):
    a,w,card,request,failure_ref=stopped_reducer(vault,monkeypatch)
    body=a.kanban.by_id[card['task_id']]['body']
    binding=json.loads(__import__('pathlib').Path(body['worker_binding']).read_text())
    value=fixed_reduce._snapshot(a,binding,{'batch_id':w['batch_id']})
    root=f'_system/ledgers/ingest-workflows/{w["workflow_id"]}'
    snapshot_ref=root+'/semantic-inputs/old.json';(vault/snapshot_ref).parent.mkdir(exist_ok=True)
    (vault/snapshot_ref).write_text(json.dumps(value))
    packet=fixed_reduce_packets.plan(a,value,fixed_semantic.prepare_request_projection)[0]
    prepared=fixed_semantic.prepare_request_projection('resource-reduce',packet['view'],schema('resource-reduce'))
    raw=json.dumps({'blocked_reason':'Recorded source QA prevents one claim being verified.'})
    home=tmp_path/'home';home.mkdir();monkeypatch.setattr(recovery.Path,'home',lambda:home.parent)
    # Runtime defaults to ~/.hermes.
    (home.parent/'.hermes').mkdir(exist_ok=True);db_path=home.parent/'.hermes/state.db'
    with sqlite3.connect(db_path) as db:
        db.executescript('CREATE TABLE sessions(id TEXT,source TEXT); CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
        db.execute("INSERT INTO sessions VALUES ('fixture','kanban')")
        db.executemany('INSERT INTO messages VALUES (?,?,?,?)',[
            ('fixture','user',prepared['wire']['user'],None),('fixture','assistant',raw,None)])
    audit={'workflow_id':w['workflow_id'],'node':card['node'],'native_task_id':card['task_id'],
        'native_run_id':'1','template_hash':binding['template_hash'],'phase':'resource-reduce',
        'schema_sha256':fingerprint(schema('resource-reduce')),'input_sha256':fingerprint(prepared['wire']),
        'user_sha256':fingerprint(prepared['wire']['user']),'response_sha256':fingerprint(raw),
        'model':{'origin':'hermes_native_model','session_id':'fixture','response_sha256':fingerprint(raw)}}
    ref=root+'/semantic-calls/refusal.json';(vault/ref).parent.mkdir(exist_ok=True);(vault/ref).write_text(json.dumps(audit))
    failure=json.loads((vault/failure_ref).read_text());failure.update(error_code='SEMANTIC_REVIEW_REQUIRED',reason=raw)
    (vault/failure_ref).write_text(json.dumps(failure))
    original_snapshot=a.kanban.task_snapshot
    def native_snapshot(*args):
        snap=original_snapshot(*args)
        for run in snap.get('runs',[]):run['worker_pid']=12
        return snap
    monkeypatch.setattr(a.kanban,'task_snapshot',native_snapshot)
    request.update(recovery_scope=recovery.SCOPE,target_contract_sha256=recovery.contract_hash())
    entry=request['entries'][0]
    entry.update(failure_sha256=hashlib.sha256((vault/failure_ref).read_bytes()).hexdigest(),
        snapshot_ref=snapshot_ref,snapshot_sha256=hashlib.sha256((vault/snapshot_ref).read_bytes()).hexdigest(),
        native_audit_ref=ref,native_audit_sha256=hashlib.sha256((vault/ref).read_bytes()).hexdigest())
    if change=='narrowed':
        w['kanban']['task_map']=[c for c in w['kanban']['task_map'] if c['node']!=card['node']];a.workflow._write(w)
    if change=='audit':entry['native_audit_sha256']='0'*64
    if change=='message':
        with sqlite3.connect(db_path) as db:db.execute("UPDATE messages SET content='tampered' WHERE role='user'")
    if change=='evidence':
        value['view']['candidates'][0]['finding']='changed';value['input_id']=fingerprint({k:v for k,v in value.items() if k!='input_id'})
        (vault/snapshot_ref).write_text(json.dumps(value));entry['snapshot_sha256']=hashlib.sha256((vault/snapshot_ref).read_bytes()).hexdigest()
    if change=='binding':a.kanban.by_id[card['task_id']]['body']['template_hash']='foreign'
    if change=='live':a.kanban.ended.clear()
    request['input_digest']=mutation_digest(request)
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    if change not in ('valid','narrowed'):
        with pytest.raises(ContractError):a.recover_reduce_execution(request)
        assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
        return
    assert not a.recover_reduce_execution(request,dry_run=True)['applied']
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    result=a.recover_reduce_execution(request);current=a.workflow.status(w['workflow_id'])
    assert result['applied'] and current['state']=='cancelled'
    revised=recovery.resolved_view(a,binding,packet['view'])
    assert revised['hold_resolution']['materials_present'] and packet['view']=={k:v for k,v in revised.items() if k!='hold_resolution'}
    assert all(p.read_bytes()==data for p,data in before.items() if '/knowledge-builds/' in p.as_posix())


def test_source_identity_ignores_only_batch_counter_and_report_list():
    original={'view':{'candidates':[{'finding':'exact fact'}],'coverage_limitations':{'excluded_task_count':2,'note':'retain loss'}},
              'backend':{'tasks':['owned'],'passes':['hash'],'coverage_gap_reports':['old']}}
    changed=copy.deepcopy(original);changed['view']['coverage_limitations']['excluded_task_count']=9
    changed['backend']['coverage_gap_reports'].append('unrelated')
    assert recovery.source_identity(changed)==recovery.source_identity(original)
    for key in ('tasks','passes'):
        bad=copy.deepcopy(changed);bad['backend'][key].append('new')
        assert recovery.source_identity(bad)!=recovery.source_identity(original)
    changed['view']['coverage_limitations']['note']='covered'
    assert recovery.source_identity(changed)!=recovery.source_identity(original)
