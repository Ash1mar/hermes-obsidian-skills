"""Mechanical readiness and partial-source repair; no semantic acceptance fixtures."""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from test_p3_knowledge_build import vault,workflow_request,ROOT
from test_reduce_execution_recovery import stopped_reducer,sync
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.reduce_dependency_revision import assert_unconsumed_resources,snapshot
from reduce_readiness import scan


def test_whole_batch_inventory_is_read_only_and_counts_all_tasks(vault,monkeypatch):
    a,w,card,request,ref=stopped_reducer(vault,monkeypatch)
    monkeypatch.setattr('native_stop_inventory.verify_cancel_drain',lambda *_a,**_k:{'actual_consumers':0})
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    result=scan(a,w['workflow_id'])
    assert result['counts']['tasks']==len(a.workflow.knowledge._batch(w['batch_id'])['task_ids'])
    assert result['counts']['task_errors']==0 and not result['issues']
    assert len(result['sources'])==2 and sum(bool(s['completed']) for s in result['sources'])==1
    assert all(s['budget']['status']=='fits' for s in result['sources'])
    assert result['native_model_calls']==0 and not result['resumed']
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    assert all(r['rendered_mapping_valid'] for r in result['tasks'])


@pytest.mark.parametrize('consumed',[False,True])
def test_dependency_revision_protects_independent_completed_source(vault,monkeypatch,consumed):
    a,w,card,_,_=stopped_reducer(vault,monkeypatch);k=a.workflow.knowledge;b=k._batch(w['batch_id'])
    w['pause_control']={'boundary':None,'evidence_digest':None,'report_ref':None,'history':[]}
    a.workflow._write(w)
    selected_source=(k._resource_reduction(b['batch_id'],b['resource_reduction_ids'][0])['resource_id'] if consumed else card['node'].partition(':')[2])
    tid=next(t for t in b['task_ids'] if k._task(t)['target_refs'][0]['unit_ref']['resource_id']==selected_source)
    prior=max(k._pass_index([tid])[1][tid],key=lambda r:r['sequence'])
    evidence=vault/'_system/reports/inventory.json';evidence.write_text(json.dumps({'task_id':tid,'scope':'isolated inventory'}))
    q=workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],revision_id='inventory-repair',
        revision_scope='reduce-dependencies/v1',evidence_digest='sha256:'+fingerprint(w['pause_control']),
        tasks=[{'task_id':tid,'pass_id':prior['pass_id'],'reason':'Required dependency in isolated fixture'}],reduce_failures=[],
        reason='Inventory before dispatch',evidence_refs=['_system/reports/inventory.json'],
        worker_template=(ROOT/'hermes-obsidian-governed-ingest-orchestrator/references/workers/pass-slice.md').read_text())
    monkeypatch.setattr('hermes_source_units.reduce_dependency_revision.citation_dependency_gaps',lambda *_a:[{'missing_ref':'isolated'}])
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    if consumed:
        with pytest.raises(ContractError,match='already consumed'):a.revise_passes(q,dry_run=True)
    else:assert a.revise_passes(q,dry_run=True)['task_count']==1
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    domain=snapshot(a.workflow,w)
    reduction_path=k._resource_reduction_path(b['batch_id'],b['resource_reduction_ids'][0]).relative_to(vault).as_posix()
    assert reduction_path in domain['files']
    if not consumed:
        completed=next(c for c in w['kanban']['task_map'] if c['node']=='resource-reduce:'+k._resource_reduction(b['batch_id'],b['resource_reduction_ids'][0])['resource_id'])
        a.revise_passes(q)
        narrowed=sync(a,w['workflow_id'])
        assert next(c for c in narrowed['kanban']['task_map'] if c['node']==completed['node'])==completed
        assert (vault/reduction_path).read_bytes()==before[vault/reduction_path]
        assert narrowed['state']=='cancelled' and narrowed['pass_revision']['state']=='running'


def test_completed_reduce_binding_survives_narrow_revision_graph(vault,monkeypatch):
    a,w,card,_,_=stopped_reducer(vault,monkeypatch)
    k=a.workflow.knowledge;b=k._batch(w['batch_id']);r=k._resource_reduction(b['batch_id'],b['resource_reduction_ids'][0])
    completed=next(c for c in w['kanban']['task_map'] if c['node']=='resource-reduce:'+r['resource_id'])
    # A genuine completed original can remain archived; projection must reuse it.
    a.kanban.archive(w['kanban']['board_id'],completed['task_id'])
    from ingest_kanban import desired_graph
    node=next(n for n in desired_graph(a.workflow,w) if n.name==completed['node'])
    assert a._completed_reduce_binding(w,node,completed['idempotency_key'])==completed['task_id']
    path=vault/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}/bindings/{fingerprint(completed["idempotency_key"])}.json'
    original=path.read_bytes();binding=json.loads(original);binding['template_hash']='sha256:'+'0'*64;path.write_text(json.dumps(binding))
    assert a._completed_reduce_binding(w,node,completed['idempotency_key']) is None
    path.write_bytes(original)


@pytest.mark.parametrize('case',['valid','usable','consumed','stale','worker'])
def test_qa_task_deferral_preserves_originals_and_never_resumes(vault,monkeypatch,case):
    a,w,card,_,_=stopped_reducer(vault,monkeypatch);k=a.workflow.knowledge;b=k._batch(w['batch_id'])
    source=card['node'].partition(':')[2]
    if case=='consumed':source=k._resource_reduction(b['batch_id'],b['resource_reduction_ids'][0])['resource_id']
    tid=next(t for t in b['task_ids'] if k._task(t)['target_refs'][0]['unit_ref']['resource_id']==source)
    prior=max(k._pass_index([tid])[1][tid],key=lambda r:r['sequence'])
    if case!='usable':
        # Isolated fixture only: append authored-looking QA history to test mechanics.
        current=copy.deepcopy(prior);current.update(sequence=prior['sequence']+1,revision_id='fixture-qa',supersedes_pass_id=prior['pass_id'])
        current['inspections'][0].update(qa='needs-qa',qa_note='Isolated original evidence limitation')
        current['pass_id']=fingerprint({f:v for f,v in current.items() if f!='pass_id'})
        root=vault/f'_system/knowledge-builds/task-{tid}/passes';(root/(current['pass_id']+'.json')).write_text(json.dumps(current))
        prior=current
        w=sync(a,w['workflow_id'])
        card=next(c for c in w['kanban']['task_map'] if c['node']==card['node'])
    failed=a.execution_failure({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id']},'SEMANTIC_REVIEW_REQUIRED','Isolated unresolved original QA')
    reviewer=vault/'_system/reports/deferral-review.json';reviewer.write_text(json.dumps({'scope':tid,'decision':'defer all candidates; gap remains'}))
    q=workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],operation_id='defer-original-qa',
        reason='Explicitly defer incomplete original evidence',evidence_refs=['_system/reports/deferral-review.json'],
        entries=[{'task_id':tid,'pass_id':prior['pass_id'],'reason':'Defer this task with explicit coverage loss',
            'failure_ref':failed,'failure_sha256':hashlib.sha256((vault/failed).read_bytes()).hexdigest()}])
    if case=='stale':q['entries'][0]['pass_id']='0'*64
    if case=='worker':monkeypatch.setenv('HERMES_KANBAN_TASK','untrusted')
    from hermes_source_units import mutation_digest
    q['input_digest']=mutation_digest(q)
    protected={p:p.read_bytes() for p in (vault/'_system/knowledge-builds').rglob('*.json') if '/passes/' in p.as_posix() or '/readings/' in p.as_posix()}
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    if case!='valid':
        with pytest.raises(ContractError):a.skip_reduce_tasks(q,dry_run=True)
        assert before=={p:p.read_bytes() for p in vault.rglob('*.json')};return
    preview=a.skip_reduce_tasks(q,dry_run=True)
    assert preview['coverage_gaps'][0]['withheld_candidate_ids'] and before=={p:p.read_bytes() for p in vault.rglob('*.json')}
    result=a.skip_reduce_tasks(q);assert result['applied'] and not result['resumed']
    value=a.workflow.status(w['workflow_id']);assert value['state']=='cancelled'
    assert a.workflow.skipped_pass_tasks(value)=={tid} and a.workflow.skipped_pass_slices(value)==set()
    assert a.workflow.verify_pass_skips(value)
    assert all(p.read_bytes()==data for p,data in protected.items())
    assert a.skip_reduce_tasks(q)['replayed']
    assert k._batch(b['batch_id'])['resource_reduction_ids']==b['resource_reduction_ids']
