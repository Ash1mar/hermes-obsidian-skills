"""Explicit same-workflow Reduce retest; retain native Passes and retired audit."""
import copy
import hashlib
import os
from pathlib import Path

from hermes_source_units import FileIngestWorkflowService
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint

CONTRACT = 'same-workflow-reduce-test/v1'


def fail(message):
    raise ContractError('REDUCE_TEST_PREFIX_INVALID', '$', message)


def checked(root, ref, digest):
    path = _vault_path(root, ref)
    if any(p.is_symlink() for p in (path, *path.parents)) or sha(path) != digest:
        fail('prefix evidence changed: ' + ref)
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def describe(origin, workflow_id):
    """Read-only plan. Pin this entire descriptor before invoking the test entry."""
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('trusted operator required')
    origin = Path(origin).resolve(strict=True)
    from ingest_kanban import IngestKanbanAdapter
    from native_stop_inventory import workflow_processes
    from fixed_semantic_evidence import pass_evidence
    adapter = IngestKanbanAdapter(origin)
    service = adapter.workflow.knowledge
    w = adapter.workflow.status(workflow_id)
    if w['state'] not in ('partial', 'completed') or workflow_processes(workflow_id, w['kanban']['board_id']):
        fail('origin must be terminal with no live consumers')
    if w.get('pass_skip_history') or w.get('pass_revision'):
        fail('this test boundary does not support skipped/revised Pass scopes')
    batch = service._batch(w['batch_id'])
    tasks = batch['task_ids']
    slices = service._slices(w['batch_id'])
    if not slices or any(s['state'] != 'completed' for s in slices):
        fail('complete accepted native Pass slices required')
    proofs = [pass_evidence(Path.home()/'.hermes', adapter, w, c['task_id'])
              for c in w['kanban']['task_map'] if c['node'].startswith('pass-slice:')]
    if any(p is None for p in proofs) or sum(p['citation_tasks'] for p in proofs) != len(tasks):
        fail('native Pass proof does not cover the exact task set')
    root = origin/f'_system/ledgers/ingest-workflows/{workflow_id}'
    files = {}
    def add(path):
        path = Path(path)
        ref = path.relative_to(origin).as_posix()
        if any(p.is_symlink() for p in (path, *path.parents)):
            fail('symlink in source prefix')
        files[ref] = sha(path)
    add(adapter.workflow._path(workflow_id)); add(service._batch_path(w['batch_id']))
    # A saved reducer snapshot fingerprints every complete pre-Reduce task.
    boundary = {}; snapshots = []
    for p in sorted((root/'semantic-inputs').glob('*.json')):
        value = _load_json(p)
        if value.get('contract') != 'fixed-reduce-input/v1' or value['backend']['kind'] != 'resource-reduce':
            continue
        if fingerprint({k:v for k,v in value.items() if k != 'input_id'}) != value['input_id']:
            fail('saved Reduce input fingerprint changed')
        backend = value['backend']
        if (backend['batch_id'] != w['batch_id'] or backend['actor'] != w['actor']
                or value['binding']['workflow_id'] != workflow_id):
            fail('Reduce snapshot owner differs')
        if len(backend['tasks']) != len(backend['task_hashes']):
            fail('incomplete pre-Reduce task fingerprints')
        for row, digest in zip(backend['tasks'], backend['task_hashes']):
            tid = row['task_id']
            if tid not in tasks:
                fail('foreign task in saved boundary')
            current = service._task(tid); add(service._task_path(tid))
            restored = copy.deepcopy(current)
            if fingerprint(restored) != digest:
                restored.update(status='running', revision=row['revision'], outputs=[], reason='')
            if fingerprint(restored) != digest or restored['status'] != 'running' or restored['outputs']:
                fail('cannot exactly authenticate pre-Reduce task: ' + tid)
            if tid in boundary and boundary[tid] != restored:
                fail('ambiguous pre-Reduce task snapshot')
            boundary[tid] = restored
        _, records = service._pass_index(r['task_id'] for r in backend['tasks'])
        if backend['passes'] != [fingerprint(p) for tid in sorted(records) for p in records[tid]]:
            fail('current Pass records differ from saved Reduce inputs')
        add(p); snapshots.append(p.relative_to(origin).as_posix())
    if set(boundary) != set(tasks):
        fail('pre-Reduce snapshots do not cover the complete task set')
    registry_revision = batch['document_registry_revision']
    registry = _load_json(origin/'_system/metadata/document-registry.json')
    organizations = _load_json(origin/'_system/metadata/source-organizations.json')
    registry_proof = None
    if registry['registry_revision'] != registry_revision:
        choices = []
        for p in (origin/'_system/metadata/source-governance-resolutions').glob('*/transaction.json'):
            transaction = _load_json(p)
            before, after = transaction['before'], transaction['after']
            if before[1]['registry_revision'] == registry_revision and after == [organizations, registry]:
                choices.append((p, before))
        if len(choices) != 1:
            fail('exact source governance boundary preimage unavailable')
        p, (organizations, registry) = choices[0]
        add(p); registry_proof = p.relative_to(origin).as_posix()
    for name in ('document-registry.json', 'source-organizations.json'):
        add(origin/'_system/metadata'/name)
    # Only source and Pass evidence is copied. Never import old downstream work.
    copy_refs = set()
    def tree(path):
        for p in path.rglob('*'):
            if p.is_symlink():
                fail('symlink in prefix tree')
            if p.is_file() and p.suffix != '.lock':
                add(p); copy_refs.add(p.relative_to(origin).as_posix())
    tree(origin/'_system/sources')
    tree(origin/'_system/reports/source-bundles')
    for outcome in w['source_outcomes']:
        if outcome['status'] != 'ready':
            fail('all prefix sources must be ready')
        for ref in [outcome['path'], *outcome['artifact_refs']]:
            add(_vault_path(origin, ref)); copy_refs.add(ref)
    for tid in tasks:
        tree(origin/f'_system/knowledge-builds/task-{tid}')
    for name in ('source-unit-config.json', 'document-governance.schema.json'):
        p = origin/'_system/metadata'/name
        if p.exists():
            add(p); copy_refs.add(p.relative_to(origin).as_posix())
    # Retain original native owner/session evidence without creating an old workflow.
    for p in (root/'semantic-calls').glob('*.json'):
        audit = _load_json(p)
        if audit['phase'] not in ('candidate', 'citation'):
            continue
        add(p); copy_refs.add(p.relative_to(origin).as_posix())
        for ref in audit['receipt_refs']:
            add(_vault_path(origin, ref)); copy_refs.add(ref)
    for name in ('native-inputs',):
        tree(root/name)
    tree(origin/f'_system/knowledge-builds/model-packets/{w["batch_id"]}')
    budget = root/'pass-call-budget.json'
    if not budget.is_file():
        fail('original cumulative Pass budget missing')
    add(budget)  # Provenance only. Never installed as new workflow credit.
    return {'contract':CONTRACT, 'origin_vault':str(origin), 'origin_workflow_id':workflow_id,
            'logical_vault_id':_load_json(origin/'_system/vault.json')['vault']['id'],
            'actor':w['actor'], 'origin_files':files, 'copy_refs':sorted(copy_refs),
            'task_ids':tasks, 'boundary_tasks':boundary, 'boundary_snapshots':snapshots,
            'registry':registry, 'organizations':organizations, 'registry_preimage_ref':registry_proof,
            'source_outcomes':w['source_outcomes'], 'native_pass_proofs':proofs,
            'inherited_pass_budget':{'ref':budget.relative_to(origin).as_posix(),'sha256':sha(budget)},
            'new_pass_calls_allowed':0, 'downstream_results_imported':False}


def _retired_files(service, w, descriptor):
    """Finite allowlist of consumed downstream data, never code/source/Pass files."""
    vault = service.vault; knowledge = service.knowledge
    root = vault/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}'
    paths = set()
    def add(path):
        if path.is_file():
            if any(p.is_symlink() for p in (path, *path.parents)):
                fail('symlink in downstream retirement')
            paths.add(path)
    def tree(path):
        if path.is_symlink():fail('symlink in downstream tree')
        for p in path.rglob('*'):
            if p.is_symlink():fail('symlink in downstream tree')
            if p.is_file() and p.suffix != '.lock':add(p)
    for name in ('page-call-budget.json','presentation-contract.json'):
        add(root/name)
    for name in ('qa-gaps','semantic-inputs','reports','decisions','output-attempts',
                 'automatic-control','automatic-release','page-drafts','build-dispositions',
                 'release-recoveries','automatic-qa'):
        tree(root/name)
    for p in (root/'semantic-calls').glob('*.json'):
        if _load_json(p).get('phase') not in ('candidate','citation'):add(p)
    # Worker receipts and bindings remain immutable historical evidence. New
    # downstream identities include the test operation, so they cannot be reused.
    tree(vault/f'_system/ledgers/knowledge-build-batches/{w["batch_id"]}/reductions')
    for p in (vault/'_system/knowledge-builds').iterdir():
        if p.name != 'model-packets' and not p.name.startswith('task-'):tree(p)
    tree(vault/'_system/knowledge-releases');tree(vault/'_system/page-governance')
    tree(vault/'_system/metadata/source-governance-resolutions')
    for subject in knowledge.list_identities()['subjects']:
        add(_vault_path(vault, subject['current_path']))
    for task in descriptor['boundary_tasks']:
        current = knowledge._task(task)
        for ref in current.get('outputs',[]):add(_vault_path(vault, ref))
        add(knowledge._task_path(task))
    for name in ('knowledge-identities.json','knowledge-release-state.json',
                 'document-registry.json','source-organizations.json'):
        add(vault/'_system/metadata'/name)
    add(vault/'_system/reports/retrieval-index-manifest.json')
    add(service._path(w['workflow_id']));add(knowledge._batch_path(w['batch_id']))
    return {p.relative_to(vault).as_posix():sha(p) for p in sorted(paths)}


def _apply(vault, journal, plan):
    """Idempotent data retirement then boundary restore, before any dispatch."""
    audit = journal.parent/'retired'
    for ref, digest in plan['retired_files'].items():
        source = _vault_path(vault, ref); target = audit/ref
        if target.exists():
            if sha(target) != digest:fail('retired audit bytes changed')
            # Once restored, a live control file legitimately differs from its
            # retired preimage. Only the recorded write below can account for it.
            if source.exists() and sha(source) == digest:source.unlink()
            elif source.exists() and ref not in plan['writes']:
                fail('unexpected live file during interrupted retirement')
            continue
        checked(vault, ref, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
    for ref, value in plan['writes'].items():
        path = _vault_path(vault, ref); data = value.encode('utf-8')
        if path.exists() and path.read_bytes() != data:
            fail('boundary changed during interrupted test admission')
        if not path.exists():_write_atomic(path, data)
    for ref, digest in plan['preserved_files'].items():checked(vault, ref, digest)
    plan['phase'] = 'admitted'
    _write_atomic(journal, _json_bytes(plan))


def _seal(journal,plan):
    row={'contract':CONTRACT,'phase':'admitted','workflow_id':plan['request']['workflow_id'],
         'operation_id':plan['request']['operation_id'],'input_digest':plan['request']['input_digest'],
         'report_ref':plan['report_ref'],'report_sha256':hashlib.sha256(plan['writes'][plan['report_ref']].encode()).hexdigest()}
    p=journal.with_name('admitted.json')
    if p.exists() and _load_json(p)!=row:fail('admission seal changed')
    if not p.exists():_write_atomic(p,_json_bytes(row))


def reenter(adapter, request, *, dry_run=False):
    """Terminal, query-disabled test Vaults only; ordinary resume never rewinds."""
    import re
    from hermes_source_units.source_units import _exclusive_lock
    from hermes_source_units.validation import validate_record
    from native_stop_inventory import workflow_processes
    service = adapter.workflow; vault = service.vault; wid = request['workflow_id']
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('trusted test operator required')
    if request.get('test_mode') != CONTRACT or request.get('overwrite_downstream') is not True:
        fail('explicit same-workflow downstream retest authorization required')
    operation = request.get('operation_id', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,70}', operation):fail('invalid stable test operation')
    service._check_request(request)
    root = vault/f'_system/ledgers/ingest-workflows/{wid}'
    journal = root/f'reduce-tests/{operation}/transaction.json'
    dispatch_lock = vault/f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock'
    with _exclusive_lock(dispatch_lock), _exclusive_lock(service._lock(wid)):
        if journal.exists():
            plan = _load_json(journal)
            if plan['request'] != request:fail('stable test operation has different inputs')
            if plan['phase'] == 'admitted':
                if not dry_run:_seal(journal,plan)
                return {'ok':True,'applied':False,'replayed':True,'workflow_id':wid,
                        'report_ref':plan['report_ref'],'resumed':False}
            if dry_run:return {'ok':True,'applied':False,'pending':True,'resumed':False}
            _apply(vault, journal, plan);_seal(journal,plan)
            return {'ok':True,'applied':True,'workflow_id':wid,'report_ref':plan['report_ref'],'resumed':False}
        w = service._mutation(request)
        if w['state'] not in ('completed','partial') or w['cancel_requested'] or w['scope'].get('pause_after'):
            fail('requires terminal auto_full test workflow without outstanding durable pauses')
        policy = _load_json(vault/'_system/metadata/retrieval-policy.json')
        if policy.get('query_enabled') is not False:
            fail('test replacement requires Query disabled')
        identity = _load_json(vault/'_system/vault.json')['vault']
        if 'test' not in identity['security_domain'].lower():fail('explicit test security domain required')
        for path in (vault/'_system/ledgers/ingest-workflows').glob('*.json'):
            if path != service._path(wid):fail('test Vault must contain only the selected workflow')
        if workflow_processes(wid,w['kanban']['board_id']):fail('stop and drain native consumers before test admission')
        states = adapter.kanban.task_states(w['kanban']['board_id'])
        if any(s in ('ready','running','todo','triage') for s in states.values()):
            fail('native board is not drained')
        descriptor = describe(vault,wid)
        if fingerprint(descriptor) != request['prefix_sha256']:
            fail('pinned pre-Reduce prefix changed')
        batch = service.knowledge._batch(w['batch_id'])
        with _exclusive_lock(service.knowledge._batch_lock_path(w['batch_id'])):
            if any(s['lease']['worker_id'] or s['state']=='leased' for s in service.knowledge._slices(w['batch_id'])):
                fail('native slice lease is still active')
            retired = _retired_files(service,w,descriptor)
            report_ref = f'_system/ledgers/ingest-workflows/{wid}/reduce-tests/{operation}/admission.json'
            report = {'contract':CONTRACT,'request':dict(request),'prefix':descriptor,
                'retired_files':retired,'budget_policy':'explicit authorized test cycle; original Pass budget unchanged; retired downstream calls remain in cumulative accounting',
                'new_pass_calls_allowed':0,'source_conversion_calls_allowed':0,
                'reused_task_count':len(descriptor['task_ids']),'native_pass_proofs':descriptor['native_pass_proofs']}
            evidence_ref=report_ref.replace('admission.json','prefix-evidence.json')
            evidence={'prefix':report.pop('prefix'),'retired_files':report.pop('retired_files'),
                      'native_pass_proofs':report.pop('native_pass_proofs')}
            report.update(evidence_ref=evidence_ref,evidence_sha256=hashlib.sha256(_json_bytes(evidence)).hexdigest())
            pointer = {'operation_id':operation,'report_ref':report_ref,'report_sha256':hashlib.sha256(_json_bytes(report)).hexdigest()}
            # Retain prefix authority and identity. All previous downstream control
            # belongs to the retired cycle rather than the new active execution.
            keys = ('contract','workflow_id','schema_version','actor','profile','scope','batch_id',
                    'pinned_inputs','dispatch_policy','kanban','source_outcomes','start_digest','template_pins')
            new = {k:copy.deepcopy(w[k]) for k in keys}
            new.update(state='cancelled',current_stage='reducing',resume_stage='reducing',cancel_requested=True,
                revision=w['revision']+1,release_id=None,release_plan_id=None,artifacts=[],
                page_execution_contract='unified-page-execution/v3',reduce_test=pointer,
                checkpoints={k:{'state':'pending','approval_digest':None,'approved_by':None,'decision_ref':None,'decision_sha256':None}
                             for k in ('checkpoint_1','checkpoint_2')})
            new['kanban']['task_map']=[c for c in new['kanban']['task_map'] if c['node'].startswith('pass-slice:')]
            newbatch = copy.deepcopy(batch)
            newbatch.update(state='cancelled',cancel_requested=True,run_ids=[],resource_reduction_ids=[],
                global_reduction_id=None,failures=[],revision=batch['revision']+1,last_operation='cancel')
            metadata = vault/'_system/metadata'
            identities = _load_json(metadata/'knowledge-identities.json')
            release = _load_json(metadata/'knowledge-release-state.json')
            identities.update(revision=0,subjects=[])
            release.update(revision=0,current_release_id=None,releases=[],applied_build_runs=[])
            writes = {report_ref:_json_bytes(report).decode(),
                evidence_ref:_json_bytes(evidence).decode(),
                service._path(wid).relative_to(vault).as_posix():_json_bytes(new).decode(),
                service.knowledge._batch_path(w['batch_id']).relative_to(vault).as_posix():_json_bytes(newbatch).decode(),
                '_system/metadata/knowledge-identities.json':_json_bytes(identities).decode(),
                '_system/metadata/knowledge-release-state.json':_json_bytes(release).decode(),
                '_system/metadata/document-registry.json':_json_bytes(descriptor['registry']).decode(),
                '_system/metadata/source-organizations.json':_json_bytes(descriptor['organizations']).decode()}
            from hermes_source_units.workflow_test import CONTRACT as NODE_TEST, POLICY
            writes[POLICY]=_json_bytes({'contract':NODE_TEST,'workflow_id':wid,'actor':w['actor'],'capture':True}).decode()
            for tid, task in descriptor['boundary_tasks'].items():
                writes[service.knowledge._task_path(tid).relative_to(vault).as_posix()]=_json_bytes(task).decode()
            validate_record('ingest_workflow',new);validate_record('knowledge_batch',newbatch)
            preserved = {ref:digest for ref,digest in descriptor['origin_files'].items() if ref not in retired}
            # Native completed slices and their immutable identity must also survive.
            for p in service.knowledge._slice_root(w['batch_id']).glob('*.json'):
                preserved[p.relative_to(vault).as_posix()]=sha(p)
            plan={'request':dict(request),'phase':'prepared','report_ref':report_ref,
                  'retired_files':retired,'preserved_files':preserved,'writes':writes}
            if dry_run:
                return {'ok':True,'applied':False,'workflow_id':wid,'retired_files':len(retired),
                        'preserved_files':len(preserved),'reused_tasks':len(descriptor['task_ids']),
                        'report_ref':report_ref,'resumed':False}
            _write_atomic(journal,_json_bytes(plan));_apply(vault,journal,plan);_seal(journal,plan)
            return {'ok':True,'applied':True,'workflow_id':wid,'report_ref':report_ref,
                    'reused_tasks':len(descriptor['task_ids']),'resumed':False}


