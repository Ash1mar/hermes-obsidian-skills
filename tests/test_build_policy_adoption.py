"""Isolated contract switching checks; no real worker/domain execution."""
import copy
import hashlib
import json

import pytest
from test_p3_knowledge_build import vault, prepare_layered_reduce_batch, workflow_request
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.build_acceptance import ADOPTION, MARKER, verify_policy_adoption
from ingest_kanban import IngestKanbanAdapter
from orchestration import worker_pack
from build_policy_adoption import target_template


class IdleBoard:
    def __init__(self):
        self.rows=[];self.runs={};self.commands=[]

    def command(self,*args):
        assert args==('kanban','--board','fixture-board','list','--archived','--json')
        self.commands.append(args);return json.dumps(self.rows)

    def task_snapshot_fresh(self,board,tid):
        return {'task':next(r for r in self.rows if r['id']==tid),'runs':self.runs.get(tid,[])}


@pytest.fixture
def held(vault,monkeypatch):
    knowledge,_,citations,snapshots=prepare_layered_reduce_batch(vault,'policy-batch')
    reduced=knowledge.reduce({'run_id':'policy-run','actor':'agent','tasks':snapshots,
        'expected_registry_revision':0,'document_registry_revision':1,
        'decisions':[{'candidate_refs':[{'pass_id':p,'candidate_id':'pump-'+str(i)}
                    for i,p in enumerate(citations.values(),1)],
            'identity':{'kind':'entity','identity_key':'pump','canonical_name':'Pump','aliases':[]},
            'action':'create','path':'30_Cards/pump.md','content':'# Pump\nSupported fixture content.\n'}],
        'reason':'Isolated checkpoint fixture'})
    knowledge._store_batch(knowledge._batch('policy-batch'),state='checkpoint_1',operation='validate',
        run_ids=[reduced['run']['run_id']],failures=[])
    adapter=IngestKanbanAdapter(vault,IdleBoard(),enable_workers=False)
    w=adapter.workflow.start(workflow_request(workflow_id='ingest-policy',actor='agent',
        expected_revision=0,profile='compact-3',batch_id='policy-batch',
        scope={'source_paths':[],'knowledge_selector':'all-current','execution_mode':'auto_full',
            'provider':'skip','pause_after':['checkpoint_1','build_finalize']}))
    pack=[{**p,'content':'<!-- hermes-fixed-semantic/v1 -->\nLegacy all-page review'}
          if p['kind']=='build-finalize' else p for p in worker_pack()]
    w=adapter.workflow.pin_templates(workflow_request(workflow_id=w['workflow_id'],actor='agent',
        expected_revision=w['revision'],templates=pack))
    w.update(state='checkpoint_1',current_stage='checkpoint_1',kanban={'board_id':'fixture-board','task_map':[]})
    w['dispatch_policy']={'mode':'full','slice_ids':[],'selection_digest':None}
    w['checkpoints']['checkpoint_1'].update(state='approved',approved_by='fixture-reviewer',approval_digest='sha256:'+'1'*64)
    adapter.workflow._write(w)
    w=adapter.workflow.latch_pause(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision']))
    monkeypatch.setattr('native_stop_inventory.workflow_processes',lambda *_:[])
    old=next(p for p in w['template_pins'] if p['kind']=='build-finalize')
    req=workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision'],
        adoption_id='program-policy',adoption_scope=ADOPTION,reason='Explicit operator authorization',
        previous_pin_digest='sha256:'+fingerprint(old),
        target_template_hash='sha256:'+hashlib.sha256(target_template()['content'].encode()).hexdigest(),
        evidence_digest=w['pause_control']['evidence_digest'])
    return adapter,w,req


def files(vault):
    return {p.relative_to(vault).as_posix():p.read_bytes() for p in vault.rglob('*') if p.is_file()}


def test_adoption_preserves_results_and_held_boundary_without_dispatch(held,vault):
    a,w,req=held;before=files(vault)
    assert not a.adopt_build_acceptance(req,dry_run=True)['applied']
    assert before==files(vault)
    result=a.adopt_build_acceptance(req)
    current=a.workflow.status(w['workflow_id'])
    assert current['revision']==w['revision']+1
    assert current['state']==current['current_stage']==a.workflow.pending_pause(current)=='checkpoint_1'
    for key in ('scope','pinned_inputs','dispatch_policy','checkpoints','kanban','artifacts'):
        assert current[key]==w[key]
    assert current['pause_control']['history']==w['pause_control']['history']
    assert current['pause_control']['evidence_digest']!=w['pause_control']['evidence_digest']
    assert not result['resumed'] and not result['background_dispatch'] and not result['pause_released']
    assert MARKER in a.workflow.pinned_templates(current)['build-finalize']
    for p,raw in before.items():
        if p!=a.workflow._path(w['workflow_id']).relative_to(vault).as_posix():
            assert (vault/p).read_bytes()==raw
    verify_policy_adoption(a.workflow,current)
    assert a.adopt_build_acceptance(req)['replayed']


def test_adoption_changes_only_future_build_identity_and_requires_new_continue_digest(held,vault):
    a,w,req=held
    from ingest_kanban import node_identity
    old_identity=node_identity(w,'build-finalize',w['batch_id'],[])
    a.adopt_build_acceptance(req);current=a.workflow.status(w['workflow_id'])
    assert node_identity(current,'build-finalize',w['batch_id'],[])!=old_identity
    for kind in ('global-reduce','resource-reduce','checkpoint-1-validate','provider-sync'):
        assert node_identity(current,kind,w['batch_id'],[])==node_identity(w,kind,w['batch_id'],[])
    stale=workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision'],
        boundary='checkpoint_1',continue_id='isolated-continue',evidence_digest=w['pause_control']['evidence_digest'])
    with pytest.raises(ContractError,match='STALE_INPUT'):a.workflow.continue_workflow(stale)
    assert a.workflow.pending_pause(a.workflow.status(w['workflow_id']))=='checkpoint_1'
    fresh={**stale,'evidence_digest':current['pause_control']['evidence_digest']}
    fresh=workflow_request(**{k:v for k,v in fresh.items() if k!='input_digest'})
    # Isolated test only; the real operator never invokes continuation here.
    assert a.workflow.continue_workflow(fresh)['pause_control']['boundary'] is None


@pytest.mark.parametrize('change,code',[
    ('actor','ACTOR_MISMATCH'),('worker','ACCESS_DENIED'),('live','WORKER_ACTIVE'),
    ('stage','INVALID_TRANSITION'),('digest','INVALID_TRANSITION'),('pin','STALE_INPUT'),
    ('target','STALE_INPUT'),('journal','ADOPTION_NOT_ALLOWED'),('mapped','ADOPTION_NOT_ALLOWED'),
    ('runnable','WORKER_ACTIVE'),('native_attempt','ADOPTION_NOT_ALLOWED'),('source','SOURCE_CHANGED')])
def test_unsafe_adoption_stops_without_writes(held,vault,monkeypatch,change,code):
    a,w,req=held
    if change=='actor':req['actor']='other'
    if change=='worker':monkeypatch.setenv('HERMES_KANBAN_TASK','worker')
    if change=='live':monkeypatch.setattr('native_stop_inventory.workflow_processes',lambda *_:[{'pid':1}])
    if change=='stage':
        w['current_stage']='build_finalizing';a.workflow._write(w)
    if change=='digest':req['evidence_digest']='sha256:'+'0'*64
    if change=='pin':req['previous_pin_digest']='sha256:'+'0'*64
    if change=='target':req['target_template_hash']='sha256:'+'0'*64
    if change=='journal':
        root=vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}/output-attempts"
        root.mkdir();(root/'unknown.json').write_text(json.dumps({'phase':'page-review','attempts':[{'state':'reserved'}]}))
    if change=='mapped':
        w['kanban']['task_map']=[{'node':'build-finalize','task_id':'old','idempotency_key':'ingest:old'}]
        a.workflow._write(w)
    if change=='runnable':a.kanban.rows=[{'id':'rogue','title':'unknown','status':'running'}]
    if change=='native_attempt':
        a.kanban.rows=[{'id':'old-build','title':'build-finalize','status':'archived'}]
        a.kanban.runs['old-build']=[{'status':'failed','outcome':'failed','started_at':1,'ended_at':2}]
    if change=='source':
        page=next((vault/'_system/knowledge-builds/policy-run/pages').glob('*.md'));page.write_text('Changed draft')
    req=workflow_request(**{k:v for k,v in req.items() if k!='input_digest'})
    before=files(vault)
    with pytest.raises(ContractError,match=code):a.adopt_build_acceptance(req)
    assert before==files(vault)


def test_interruption_before_pointer_commit_replays_the_exact_request(held,vault,monkeypatch):
    a,w,req=held
    with monkeypatch.context() as patch:
        patch.setattr(a.workflow,'_write',lambda *_:(_ for _ in ()).throw(OSError('Isolated pointer interruption')))
        with pytest.raises(OSError):a.adopt_build_acceptance(req)
    assert a.workflow.status(w['workflow_id'])==w
    assert a.adopt_build_acceptance(req)['applied']
    assert a.adopt_build_acceptance(req)['replayed']


@pytest.mark.parametrize('part',['audit','old_pause','new_pause','template'])
def test_tampered_adoption_evidence_blocks_resume_and_continuation(held,vault,part):
    a,w,req=held;result=a.adopt_build_acceptance(req);current=a.workflow.status(w['workflow_id'])
    paths={'audit':result['report_ref'],'old_pause':w['pause_control']['report_ref'],
        'new_pause':current['pause_control']['report_ref'],'template':result['target_pin']['path']}
    (vault/paths[part]).write_text('tampered')
    with pytest.raises(ContractError):a.workflow.assert_execution_ready(current)
    continuation=workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision'],
        boundary='checkpoint_1',continue_id='isolated',evidence_digest=current['pause_control']['evidence_digest'])
    with pytest.raises(ContractError):a.workflow.continue_workflow(continuation)


def test_second_adoption_id_cannot_replace_the_recorded_contract(held):
    a,w,req=held;a.adopt_build_acceptance(req);current=a.workflow.status(w['workflow_id'])
    another=workflow_request(**{**{k:v for k,v in req.items() if k!='input_digest'},
        'adoption_id':'new-id','expected_revision':current['revision']})
    with pytest.raises(ContractError,match='IDEMPOTENCY_CONFLICT'):a.adopt_build_acceptance(another)


def batch_counter_drift(held,vault):
    """Simulate approval's revalidation, then one later unchanged validation."""
    from hermes_source_units.source_units import _json_bytes
    a,w,req=held
    batch=a.workflow.knowledge._batch(w['batch_id'])
    approved={**batch,'revision':batch['revision']-1}
    ref=f"_system/ledgers/ingest-workflows/{w['workflow_id']}/decisions/approved-batch.json"
    path=vault/ref;path.parent.mkdir(parents=True)
    decision={'contract':'hermes-ingest-checkpoint-decision/v1','workflow_id':w['workflow_id'],
        'checkpoint':'checkpoint_1','approval_digest':w['checkpoints']['checkpoint_1']['approval_digest'],
        'ok':True,'blocking_codes':[],'evidence':{'batch':approved}}
    path.write_bytes(_json_bytes(decision))
    w['checkpoints']['checkpoint_1'].update(decision_ref=ref,decision_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    a.workflow._write(w)
    evidence=a.workflow._pause_evidence(w,'checkpoint_1')
    saved={'contract':'hermes-ingest-pause/v1','evidence_digest':'sha256:'+fingerprint(evidence),'evidence':evidence}
    pause_ref=f"_system/ledgers/ingest-workflows/{w['workflow_id']}/pauses/counter-drift.json"
    (vault/pause_ref).write_bytes(_json_bytes(saved))
    w['pause_control'].update(report_ref=pause_ref,evidence_digest=saved['evidence_digest'])
    a.workflow._write(w)
    # Only the revision changes, as validate_batch does for identical input.
    a.workflow.knowledge._batch_path(w['batch_id']).write_bytes(_json_bytes({**batch,'revision':batch['revision']+1}))
    req=workflow_request(**{**{k:v for k,v in req.items() if k!='input_digest'},
        'evidence_digest':saved['evidence_digest']})
    return a,w,req


def test_exact_revision_only_drift_is_proven_from_approved_checkpoint(held,vault):
    a,w,req=batch_counter_drift(held,vault)
    before=a.workflow.knowledge._batch_path(w['batch_id']).read_bytes()
    result=a.adopt_build_acceptance(req)
    report=json.loads((vault/result['report_ref']).read_text())
    proof=report['batch_metadata_refresh']
    assert proof['paused_revision']==proof['approved_revision']+1
    assert proof['current_revision']==proof['paused_revision']+1
    assert a.workflow.knowledge._batch_path(w['batch_id']).read_bytes()==before
    current=a.workflow.status(w['workflow_id'])
    verify_policy_adoption(a.workflow,current)
    assert a.workflow.pending_pause(current)=='checkpoint_1'
    assert a.workflow._pause_evidence(current,'checkpoint_1')==json.loads((vault/current['pause_control']['report_ref']).read_text())['evidence']


@pytest.mark.parametrize('field,value',[('last_operation','reduce'),('cooldown_reason','different'),('revision',1)])
def test_revision_compatibility_never_accepts_other_batch_changes(held,vault,field,value):
    from hermes_source_units.source_units import _json_bytes
    a,w,req=batch_counter_drift(held,vault)
    path=a.workflow.knowledge._batch_path(w['batch_id']);batch=json.loads(path.read_text())
    batch[field]=value;path.write_bytes(_json_bytes(batch));before=files(vault)
    with pytest.raises(ContractError,match='STALE_INPUT'):a.adopt_build_acceptance(req)
    assert before==files(vault)
