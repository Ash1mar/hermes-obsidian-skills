"""One checked operation from typed semantic payload to durable Pass receipt."""
from hermes_source_units import ContractError, FileKnowledgeBuildService
from hermes_source_units.model_projection import RENDERER
from hermes_source_units.semantic_submission import validate_submission
from hermes_source_units.source_units import _vault_path, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard, WORKER_LOCK_TIMEOUT
from ingest_kanban import IngestKanbanAdapter


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
    with worker_binding(binding), workflow_write_guard(vault, kinds=('pass-slice',), actor=binding['actor']):
        checked = adapter.worker_check(binding)
        if service._slice(checked['batch_id'], checked['slice_id'])['template_id'] != RENDERER:
            raise ContractError('ACCESS_DENIED', '$', 'typed submission requires a bounded model slice')
        reviews = []
        if confirmation:
            expanded, reviews = service.expand_citation_confirmations(request, checked['task_ids'], binding['actor'], checked['batch_id'])
        else:
            expanded = service.expand_model_passes(request, checked['task_ids'], binding['actor'], checked['batch_id'])
        audit = {'worker_request_digest': fingerprint(binding), 'workflow_id': binding['workflow_id'],
                 'node': binding['node'], 'worker_id': binding['worker_id'],
                 'lease_revision': binding['expected_revision'], 'semantic_request_digest': fingerprint(request)}
        result = service.record_pass_batch({**expanded, 'slice_id': checked['slice_id'],
            'worker_id': binding['worker_id'], 'template_hash': binding['template_hash']},
            lock_timeout=WORKER_LOCK_TIMEOUT, lock_check=lambda: adapter.worker_check(binding), submission_audit=audit)
        aliases = {tid: f't{i}' for i, tid in enumerate(checked['task_ids'], 1)}
        receipts = []
        for r in result['results']:
            root = _vault_path(service.vault, f'_system/knowledge-builds/task-{r["task_id"]}/passes')
            receipt = {**r, 'task': aliases[r['task_id']], 'next_sequence': len(list(root.glob('*.json'))),
                       'pass_kind': 'candidate' if r['sequence'] == 0 else 'citation',
                       'next_action': 'review_candidate' if r['sequence'] == 0 else 'pass_complete'}
            review = next((v for v in reviews if v['task_id'] == r['task_id']), None)
            if review:
                evidence = {'contract': 'hermes-citation-confirmation/v1', 'binding': audit,
                            'review': review, 'citation_pass_id': r['pass_id']}
                ref = f'_system/knowledge-builds/task-{r["task_id"]}/citation-reviews/{fingerprint(evidence)}.json'
                _write_atomic(_vault_path(service.vault, ref), _json_bytes(evidence))
                receipt['review_ref'] = ref
            receipts.append(receipt)
        return {'ok': result['ok'], 'validated': result['ok'], 'results': receipts, 'failures': result['failures']}
