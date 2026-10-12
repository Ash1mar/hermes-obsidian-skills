"""One checked operation from typed semantic payload to durable Pass receipt."""
from hermes_source_units import ContractError, FileKnowledgeBuildService
from hermes_source_units.model_projection import RENDERER
from hermes_source_units.semantic_submission import validate_submission
from hermes_source_units.source_units import _vault_path, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard, WORKER_LOCK_TIMEOUT
from ingest_kanban import IngestKanbanAdapter, semantic_template


def submit_bound(vault, binding, request, *, confirmation=False, adapter=None):
    # Shape failures happen before lease renewal, guard entry or any domain write.
    validate_submission(request, confirmation=confirmation)
    adapter = adapter or IngestKanbanAdapter(vault, enable_workers=True)
    adapter.validate_worker_request(binding)
    if not binding['node'].startswith('pass-slice:'):
        raise ContractError('ACCESS_DENIED', '$', 'semantic submission requires a Pass worker')
    service = FileKnowledgeBuildService(vault)
    # Expansion also checks these same repositories. Enable the existing
    # signature-checked invocation cache before expansion, retaining ACL and
    # live pre-commit checks, instead of enabling it only during persistence.
    service.source.enable_session_cache()
    checked = adapter.worker_check(binding)
    workflow = adapter.workflow.status(binding['workflow_id'])
    effective = (adapter.workflow.execution_template(workflow, checked['slice_id'])
                 or adapter.workflow.pinned_templates(workflow)['pass-slice'])
    if (service._slice(checked['batch_id'], checked['slice_id'])['template_id'] != RENDERER
            and not semantic_template('pass-slice', effective)):
        raise ContractError('ACCESS_DENIED', '$', 'typed submission requires a bounded model slice')
    # Pure reading/identity expansion does not need the shared cancellation lock.
    # Persistence still verifies live source/ACL, task revision and current lease.
    reviews = []
    if confirmation:
        expanded, reviews = service.expand_citation_confirmations(request, checked['task_ids'], binding['actor'], checked['batch_id'])
    else:
        expanded = service.expand_model_passes(request, checked['task_ids'], binding['actor'], checked['batch_id'])
    audit = {'worker_request_digest': fingerprint(binding), 'workflow_id': binding['workflow_id'],
             'node': binding['node'], 'worker_id': binding['worker_id'],
             'lease_revision': binding['expected_revision'], 'semantic_request_digest': fingerprint(request)}
    aliases = {tid: f't{i}' for i, tid in enumerate(checked['task_ids'], 1)}
    receipts=[];failures=[]
    # Passes already have individual durable preflights and partial recovery.
    # Hold cancellation serialization for one atomic Pass, not the whole group.
    for index,item in enumerate(expanded['passes']):
        try:
            live=adapter.worker_check(binding,verify_inputs=False)
            if live!=checked:
                raise ContractError('STALE_INPUT','$','bound slice changed after read-only expansion')
            with worker_binding(binding), workflow_write_guard(vault,kinds=('pass-slice',),actor=binding['actor']):
                result=service.record_pass_batch({**expanded,'passes':[item],'slice_id':checked['slice_id'],
                    'worker_id':binding['worker_id'],'template_hash':binding['template_hash']},
                    lock_timeout=WORKER_LOCK_TIMEOUT,lock_check=lambda:adapter.worker_check(binding,verify_inputs=False),submission_audit=audit)
                failures.extend(result['failures'])
                for r in result['results']:
                    root = _vault_path(service.vault, f'_system/knowledge-builds/task-{r["task_id"]}/passes')
                    receipt = {**r, 'task': aliases[r['task_id']], 'next_sequence': len(list(root.glob('*.json'))),
                               'pass_kind': 'candidate' if r['sequence'] == 0 else 'citation',
                               'next_action': 'review_candidate' if r['sequence'] == 0 else 'pass_complete'}
                    review=next((v for v in reviews if v['task_id']==r['task_id']),None)
                    if review:
                        evidence={'contract':'hermes-citation-confirmation/v1','binding':audit,
                                  'review':review,'citation_pass_id':r['pass_id']}
                        ref=f'_system/knowledge-builds/task-{r["task_id"]}/citation-reviews/{fingerprint(evidence)}.json'
                        _write_atomic(_vault_path(service.vault,ref),_json_bytes(evidence))
                        receipt['review_ref']=ref
                    receipts.append(receipt)
        except (ContractError,OSError,ValueError,TypeError,KeyError) as exc:
            failures.extend(service._failure('pass',pending['task_id'],exc) for pending in expanded['passes'][index:])
            break  # Changed authority/lock failure never retries a mutation.
    return {'ok':not failures,'validated':not failures,'results':receipts,'failures':failures}
