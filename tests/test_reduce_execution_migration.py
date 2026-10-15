"""Isolated migration checks; these fixtures are never production domain results."""
import copy
import hashlib
import json

import pytest

from test_p3_knowledge_build import vault, prepare_layered_reduce_batch, workflow_request
from test_pass_revision import ArchivedKeyKanban
from ingest_kanban import IngestKanbanAdapter, desired_graph
from orchestration import worker_pack
from reduce_execution_migration import KINDS, target_hashes
from hermes_source_units import ContractError, mutation_digest
from hermes_source_units.validation import fingerprint


class MigrationKanban(ArchivedKeyKanban):
    def __init__(self):
        super().__init__()
        self.native_runs={}

    def task_states(self, slug):
        return {tid:self.task_snapshot(slug,tid)['task']['status'] for tid in self.by_id}

    def task_snapshot(self, slug, tid):
        value=super().task_snapshot(slug,tid)
        value['runs']=self.native_runs.get(tid,[])
        return value


@pytest.fixture
def legacy(vault, monkeypatch):
    prepare_layered_reduce_batch(vault,'migration-batch')
    a=IngestKanbanAdapter(vault,MigrationKanban(),enable_workers=True)
    value=a.workflow.start(workflow_request(workflow_id='ingest-reduce-migration',actor='agent',
        expected_revision=0,profile='compact-3',batch_id='migration-batch',scope={
            'source_paths':[],'knowledge_selector':'all-current','execution_mode':'auto_full',
            'provider':'skip','pause_after':['checkpoint_1']}))
    pack=[{**p,'content':'Legacy model worker: '+p['kind']} if p['kind'] in KINDS else p for p in worker_pack()]
    value=a.workflow.pin_templates(workflow_request(workflow_id=value['workflow_id'],actor='agent',
        expected_revision=value['revision'],templates=pack))
    value['dispatch_policy']={'mode':'full','slice_ids':[],'selection_digest':None}
    a.workflow._write(value)
    def sync():
        w=a.workflow.status(value['workflow_id'])
        a.sync(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision']))
        return a.workflow.status(w['workflow_id'])
    value=sync()
    for card in value['kanban']['task_map']:
        if card['node'].startswith('pass-slice:'):
            begun=a.worker_begin({'workflow_id':value['workflow_id'],'node':card['node'],
                                  'task_id':card['task_id'],'worker_id':'fixture'})
            assert a.worker_complete(begun['worker_request'])['ok']
    value=sync()
    a.cancel(workflow_request(workflow_id=value['workflow_id'],actor='agent',expected_revision=value['revision']))
    value=a.workflow.status(value['workflow_id'])
    monkeypatch.setattr('native_stop_inventory.workflow_processes',lambda *_:[])
    request=workflow_request(workflow_id=value['workflow_id'],actor='agent',expected_revision=value['revision'],
        migration_id='legacy-fixed-reduce',migration_scope='reduce-fixed/v1',reason='Explicit authorized migration',
        previous_pins_digest='sha256:'+fingerprint([p for p in value['template_pins'] if p['kind'] in KINDS]),
        target_template_hashes=target_hashes())
    return a,value,request


def test_migration_preserves_passes_pins_and_pauses_and_rebinds_only_reduce(legacy,vault):
    a,w,request=legacy
    original={p:p.read_bytes() for p in vault.rglob('*.json')}
    domain={p:p.read_bytes() for p in (vault/'_system/knowledge-builds').rglob('*') if p.is_file()}
    old_templates={p['path']:(vault/p['path']).read_bytes() for p in w['template_pins']}
    old_pass=[c for c in w['kanban']['task_map'] if c['node'].startswith('pass-slice:')]
    assert not a.migrate_reduce_execution(request,dry_run=True)['applied']
    assert original=={p:p.read_bytes() for p in vault.rglob('*.json')}
    result=a.migrate_reduce_execution(request)
    current=a.workflow.status(w['workflow_id'])
    assert current['state']=='cancelled' and not result['resumed']
    assert current['scope']==w['scope'] and current.get('pause_control')==w.get('pause_control')
    assert current['kanban']==w['kanban']
    assert a.migrate_reduce_execution(request)['replayed']
    assert all((vault/p).read_bytes()==b for p,b in old_templates.items())
    assert all(p.read_bytes()==b for p,b in domain.items())
    a.resume(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
    current=a.workflow.status(w['workflow_id'])
    assert [c for c in current['kanban']['task_map'] if c['node'].startswith('pass-slice:')]==old_pass
    assert all(p.read_bytes()==b for p,b in domain.items() if 'passes' in p.parts or 'readings' in p.parts)
    assert not any(c['node']=='build-finalize' for c in current['kanban']['task_map'])
    for old in [c for c in w['kanban']['task_map'] if c not in old_pass]:
        fresh=next(c for c in current['kanban']['task_map'] if c['node']==old['node'])
        assert fresh['task_id']!=old['task_id']
        binding=json.loads((vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}/bindings/{fingerprint(old['idempotency_key'])}.json").read_text())
        with pytest.raises(ContractError,match='STALE_INPUT'):
            a.worker_check(binding)


@pytest.mark.parametrize('change,code',[
    ('actor','ACTOR_MISMATCH'),('worker','ACCESS_DENIED'),('live','WORKER_ACTIVE'),
    ('old_pins','STALE_INPUT'),('new_pins','STALE_INPUT'),('attempt','MIGRATION_NOT_ALLOWED'),
    ('failure','MIGRATION_NOT_ALLOWED'),('stage','INVALID_TRANSITION'),('completed','MIGRATION_NOT_ALLOWED')])
def test_migration_rejects_unsafe_or_stale_work_without_writes(legacy,vault,monkeypatch,change,code):
    a,w,request=legacy
    card=next(c for c in w['kanban']['task_map'] if c['node'].startswith('resource-reduce:'))
    if change=='actor':request['actor']='other'
    if change=='worker':monkeypatch.setenv('HERMES_KANBAN_TASK','worker')
    if change=='live':monkeypatch.setattr('native_stop_inventory.workflow_processes',lambda *_:[{'pid':1}])
    if change=='old_pins':request['previous_pins_digest']='sha256:'+'0'*64
    if change=='new_pins':request['target_template_hashes']={}
    if change=='attempt':a.kanban.native_runs[card['task_id']]=[{'status':'failed','started_at':1,'ended_at':2}]
    if change=='failure':a.execution_failure({'workflow_id':w['workflow_id'],**{k:card[k] for k in ('node','task_id')}},'READING_WINDOW_OVERSIZE','Isolated refusal')
    if change=='stage':w['current_stage']='analyzing';a.workflow._write(w)
    if change=='completed':w['checkpoints']['checkpoint_1']['state']='approved';a.workflow._write(w)
    request['input_digest']=mutation_digest(request)
    before={p:p.read_bytes() for p in vault.rglob('*.json')}
    with pytest.raises(ContractError,match=code):a.migrate_reduce_execution(request)
    assert before=={p:p.read_bytes() for p in vault.rglob('*.json')}


def test_migration_interrupted_before_pointer_commit_can_replay_same_request(legacy,monkeypatch):
    a,w,request=legacy
    original=a.workflow._write
    with monkeypatch.context() as patch:
        patch.setattr(a.workflow,'_write',lambda *_:(_ for _ in ()).throw(OSError('Isolated pointer interruption')))
        with pytest.raises(OSError):a.migrate_reduce_execution(request)
    assert a.workflow.status(w['workflow_id'])==w
    assert a.migrate_reduce_execution(request)['applied']


def test_migration_evidence_tamper_holds_resume_and_second_migration_is_rejected(legacy,vault):
    a,w,request=legacy
    result=a.migrate_reduce_execution(request)
    current=a.workflow.status(w['workflow_id'])
    other=copy.deepcopy(request)
    other.update(migration_id='another',expected_revision=current['revision'])
    other['input_digest']=mutation_digest(other)
    with pytest.raises(ContractError,match='IDEMPOTENCY_CONFLICT'):a.migrate_reduce_execution(other)
    (vault/result['report_ref']).write_text('{}')
    with pytest.raises(ContractError,match='STALE_INPUT'):
        a.resume(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
