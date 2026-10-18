"""Isolated result lineage and selected native packet authority; no real data."""
import copy,hashlib,json,sqlite3
import pytest
from test_p3_knowledge_build import vault,workflow_request
from test_reduce_execution_recovery import stopped_reducer,sync
from hermes_source_units import ContractError,mutation_digest
from hermes_source_units.validation import fingerprint
from ingest_kanban import desired_graph,domain_completed
import fixed_reduce,source_result_review as review


def fixture(vault,monkeypatch):
    a,w,_,req,_=stopped_reducer(vault,monkeypatch)
    card=next(c for c in w['kanban']['task_map'] if c['node'].startswith('resource-reduce:') and
              domain_completed(a.workflow,w,next(n for n in desired_graph(a.workflow,w) if n.name==c['node'])))
    k=a.workflow.knowledge;batch=k._batch(w['batch_id']);rid=batch['resource_reduction_ids'][0];record=k._resource_reduction(batch['batch_id'],rid)
    root=vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}"
    path,old=next((p,json.loads(p.read_text())) for p in (root/'semantic-inputs').glob('*.json')
                  if json.loads(p.read_text()).get('binding',{}).get('task_id')==card['task_id'])
    binding=old['binding'];handles=[c['candidate'] for c in old['view']['candidates']]
    draft={'proposals':[{'candidate_refs':handles,'summary':'Isolated fixture grouping','identity_hints':[],'path_hints':[]}],
           'omitted_candidate_refs':[],'reason':'Isolated mechanism fixture'}
    # The earlier fixture submits directly; construct the packet-aggregate form
    # in this isolated Vault so the review checks the real aggregate contract.
    from fixed_reduce_packets import merge
    record['reason']=merge(old,[{'candidate_refs':handles}],[draft])['reason']
    record['output_fingerprint']='sha256:'+fingerprint({key:record[key] for key in ('proposals','omitted_candidate_refs','reason')})
    k._resource_reduction_path(batch['batch_id'],rid).write_text(json.dumps(record))
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_schema import schema
    wire=prepare_request_projection('resource-reduce',old['view'],schema('resource-reduce'),[])['wire'];raw=json.dumps(draft)
    home=vault/'fixture-home';home.mkdir();monkeypatch.setattr(review,'native_home',lambda:home)
    with sqlite3.connect(home/'state.db') as db:
        db.executescript('CREATE TABLE sessions(id TEXT,source TEXT);CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
        db.execute('INSERT INTO sessions VALUES (?,?)',('native-fixture','kanban'))
        db.executemany('INSERT INTO messages VALUES (?,?,?,?)',[('native-fixture','user',wire['user'],None),('native-fixture','assistant',raw,None)])
    receipt=next(p for p in (root/'worker-receipts').glob('reduce-*.json') if json.loads(p.read_text()).get('reduction_id')==rid)
    audit={'native_task_id':card['task_id'],'native_run_id':'1','workflow_id':w['workflow_id'],'node':card['node'],
           'template_hash':binding['template_hash'],'phase':'resource-reduce','validated':True,
           'model':{'origin':'hermes_native_model','session_id':'native-fixture','response_sha256':fingerprint(raw)},
           'input_sha256':fingerprint(wire),'user_sha256':fingerprint(wire['user']),'response_sha256':fingerprint(raw),
           'schema_sha256':fingerprint(schema('resource-reduce')),'receipt_refs':[receipt.relative_to(vault).as_posix()]}
    (root/'semantic-calls').mkdir(exist_ok=True);audit_path=root/'semantic-calls/source.json';audit_path.write_text(json.dumps(audit))
    original=a.kanban.task_snapshot
    def snap(slug,tid):
        s=original(slug,tid)
        if tid==card['task_id']:s['runs']=[{'id':1,'started_at':1,'ended_at':2,'worker_pid':999}]
        return s
    monkeypatch.setattr(a.kanban,'task_snapshot',snap)
    req.update(recovery_scope=review.SCOPE,target_contract_sha256=review.contract_hash(),entries=[{
        'node':card['node'],'task_id':card['task_id'],'reduction_id':rid,'reduction_sha256':hashlib.sha256(k._resource_reduction_path(batch['batch_id'],rid).read_bytes()).hexdigest(),
        'snapshot_ref':path.relative_to(vault).as_posix(),'snapshot_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'selected_audits':[{'ref':audit_path.relative_to(vault).as_posix(),'sha256':hashlib.sha256(audit_path.read_bytes()).hexdigest()}]}])
    req['input_digest']=mutation_digest(req)
    return a,w,card,req,record,draft,audit_path


@pytest.mark.parametrize('change',['valid','consumed','audit','foreign'])
def test_review_authenticates_formal_native_result_and_never_changes_old_domain(vault,monkeypatch,change):
    a,w,card,req,record,draft,audit_path=fixture(vault,monkeypatch)
    if change=='consumed':
        b=a.workflow.knowledge._batch(w['batch_id']);b['global_reduction_id']='a'*64;a.workflow.knowledge._batch_path(w['batch_id']).write_text(json.dumps(b))
    if change=='audit':req['entries'][0]['selected_audits'][0]['sha256']='0'*64
    if change=='foreign':req['entries'][0]['selected_audits'][0]['ref']=req['entries'][0]['selected_audits'][0]['ref'].replace('source.json','foreign.json')
    req['input_digest']=mutation_digest(req);before={p:p.read_bytes() for p in vault.rglob('*.json')}
    if change!='valid':
        with pytest.raises(ContractError):a.recover_reduce_execution(req)
        assert before=={p:p.read_bytes() for p in vault.rglob('*.json')};return
    assert a.recover_reduce_execution(req,dry_run=True)['ok']
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    a.recover_reduce_execution(req);current=a.workflow.status(w['workflow_id'])
    assert not domain_completed(a.workflow,current,next(n for n in desired_graph(a.workflow,current) if n.name==card['node']))
    a.resume(workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=current['revision']))
    current=sync(a,w['workflow_id']);fresh=next(c for c in current['kanban']['task_map'] if c['node']==card['node'])
    assert fresh['task_id']!=card['task_id']
    binding=json.loads(open(a.kanban.by_id[fresh['task_id']]['body']['worker_binding']).read());ref,value=fixed_reduce.prepare(a,binding)
    altered=copy.deepcopy(draft);altered['proposals'][0]['summary']='Isolated corrected source grouping'
    result=fixed_reduce.submit(a,binding,ref,altered);assert result['validated']
    k=a.workflow.knowledge;batch=k._batch(w['batch_id']);assert record['reduction_id'] not in batch['resource_reduction_ids']
    new=k._resource_reduction(w['batch_id'],batch['resource_reduction_ids'][0]);assert new['supersedes_reduction_id']==record['reduction_id']
    assert json.loads(k._resource_reduction_path(w['batch_id'],record['reduction_id']).read_text())==record
    assert all(p.read_bytes()==data for p,data in before.items() if 'passes' in p.parts or 'readings' in p.parts)


def test_replacement_without_bound_review_authority_is_rejected(vault,monkeypatch):
    a,w,card,req,record,draft,_=fixture(vault,monkeypatch)
    from hermes_source_units.resource_amendment import authorize
    with pytest.raises(ContractError,match='ACCESS_DENIED'):authorize(a.workflow.knowledge,record['resource_id'],record['reduction_id'])
