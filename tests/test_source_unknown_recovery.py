"""Hash-bound stopped source quarantine in isolated fixture Vaults only."""
import copy,json,sqlite3
from pathlib import Path
import pytest
from test_p3_knowledge_build import vault
from test_reduce_execution_recovery import stopped_reducer
from hermes_source_units import ContractError,mutation_digest
from hermes_source_units.validation import fingerprint
from hermes_source_units.recovery_policy import DEFAULT
import source_unknown_recovery as recovery
import fixed_reduce,fixed_reduce_packets,fixed_semantic
from fixed_semantic_schema import schema
from semantic_output import path_for

def fixture(vault,monkeypatch,tmp_path):
    a,w,card,req,fr=stopped_reducer(vault,monkeypatch)
    w=a.workflow.status(w['workflow_id']);w['scope']['recovery_policy']={**DEFAULT,'unknown_completion':'isolate_after_drain'};a.workflow._write(w)
    binding=json.loads(Path(a.kanban.by_id[card['task_id']]['body']['worker_binding']).read_text())
    value=fixed_reduce._snapshot(a,binding,{'batch_id':w['batch_id']},presentation=False)
    root=vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}";sp=root/'semantic-inputs/original.json';sp.parent.mkdir(exist_ok=True);sp.write_text(json.dumps(value))
    packet=fixed_reduce_packets.plan(a,value,fixed_semantic.prepare_request_projection)[0]
    view=packet['view'];sc=schema('resource-reduce');prepared=fixed_semantic.prepare_request_projection('resource-reduce',view,sc)
    jp=path_for(a,binding,'resource-reduce',view);jp.parent.mkdir(exist_ok=True)
    attempt={'attempt':1,'attempt_id':fingerprint({'path':jp.name,'attempt':1}),'task_id':binding['task_id'],'run_id':'1','session_id':'unknown-fixture','wire_sha256':fingerprint(prepared['wire']),'state':'reserved'}
    rp=root/'call-failures/unknown.json';rp.parent.mkdir(exist_ok=True)
    report={'contract':'native-call-outcome/v1','state':'completion_unknown','session_id':attempt['session_id'],'attempt_id':attempt['attempt_id'],'task_id':binding['task_id'],'run_id':'1','phase':'resource-reduce','schema_sha256':fingerprint(sc),'view':view,'wire':prepared['wire'],'completed_output':False,'image_inputs':[]}
    rp.write_text(json.dumps(report));attempt['call_outcome']={'state':'completion_unknown','report_ref':'call-failures/unknown.json','report_sha256':recovery.sha(rp)}
    jp.write_text(json.dumps({'contract':'finite-semantic-output/v1','phase':'resource-reduce','input_sha256':fingerprint(view),'schema_sha256':fingerprint(sc),'attempts':[attempt]}))
    home=tmp_path/'fake-home';(home/'.hermes').mkdir(parents=True);monkeypatch.setattr(Path,'home',classmethod(lambda _:home))
    with sqlite3.connect(home/'.hermes/state.db') as db:
        db.executescript('CREATE TABLE sessions(id TEXT,source TEXT);CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
        db.execute('INSERT INTO sessions VALUES (?,?)',(attempt['session_id'],'kanban'))
        db.execute('INSERT INTO messages VALUES (?,?,?,?)',(attempt['session_id'],'user',prepared['wire']['user'],None))
    fail=json.loads((vault/fr).read_text());fail.update(error_code='NATIVE_COMPLETION_UNKNOWN',reason='isolated stream unknown');(vault/fr).write_text(json.dumps(fail))
    req.update(target_contract_sha256=recovery.contract_hash())
    req['entries'][0].update(source_unknown_isolation=True,failure_sha256=recovery.sha(vault/fr),snapshot_ref=sp.relative_to(vault).as_posix(),snapshot_sha256=recovery.sha(sp))
    req['input_digest']=mutation_digest(req)
    return a,w,card,binding,value,packet,req,jp,rp,home

@pytest.mark.parametrize('change',[None,'snapshot','journal','answer','policy','contract','run'])
def test_source_unknown_admission_is_exact_and_does_not_consume_budget(vault,monkeypatch,tmp_path,change):
    a,w,c,b,value,packet,req,jp,rp,home=fixture(vault,monkeypatch,tmp_path)
    if change=='snapshot':req['entries'][0]['snapshot_sha256']='0'*64
    if change=='journal':
        j=json.loads(jp.read_text());j['attempts'][0]['wire_sha256']='changed';jp.write_text(json.dumps(j))
    if change=='answer':
        with sqlite3.connect(home/'.hermes/state.db') as db:db.execute('INSERT INTO messages VALUES (?,?,?,?)',('unknown-fixture','assistant','returned answer',None))
    if change=='policy':
        now=a.workflow.status(w['workflow_id']);now['scope']['recovery_policy']['unknown_completion']='hold';a.workflow._write(now)
    if change=='contract':req['target_contract_sha256']='0'*64
    if change=='run':a.kanban.ended.clear()
    req['input_digest']=mutation_digest(req);before=jp.read_bytes()
    if change:
        with pytest.raises(ContractError):a.recover_reduce_execution(req,dry_run=True)
    else:
        preview=a.recover_reduce_execution(req,dry_run=True);proof=preview['entries'][0]['source_unknown_isolation']
        assert proof['new_model_calls_granted']==0 and proof['unknowns_preserved'] and not proof['coverage_complete']
        result=a.recover_reduce_execution(req);assert not result['resumed']
        assert a.recover_reduce_execution(req)['replayed']
        # Read the sealed omission without starting a worker or calling a model.
        isolated=recovery.packet_disposition(a,b,value,packet)
        assert isolated['candidate_refs']==packet['candidate_refs'] and isolated['reserved_ordinal']==1
        jp.write_bytes(before+b' ')
        with pytest.raises(ContractError,match='budget'):recovery.packet_disposition(a,b,value,packet)
        jp.write_bytes(before)
    assert jp.read_bytes()==before

def test_operator_cancel_and_hold_policy_do_not_autoresume(tmp_path,monkeypatch):
    from types import SimpleNamespace as NS
    for policy in ('hold','isolate_after_drain'):
        w={'workflow_id':'w','state':'cancelled','current_stage':'reducing','cancel_requested':True,'scope':{'execution_mode':'auto_full','recovery_policy':{**DEFAULT,'unknown_completion':policy}}}
        a=NS(workflow=NS(vault=tmp_path,status=lambda _:w,pending_pause=lambda _:False))
        assert recovery.recover(a,'w') is False
