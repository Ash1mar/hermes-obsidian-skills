"""Exact Bootstrap/source/Build authority for a Vault's first internal release.

An initial grant names a rejected draft plan, never an invented applied release.
It grants no semantic retry, source reset, public access or QA clearance.
"""
from .source_units import _vault_path
from .partial_finalize import pinned
from .validation import ContractError, fingerprint, validate_record

CONTRACT = 'governed-initial-release-authorization/v1'


def needs_preflight(vault, workflow):
    scope=workflow.get('scope',{})
    return (scope.get('execution_mode')=='auto_full' and bool(scope.get('bootstrap_ref'))
        and scope.get('recovery_policy',{}).get('initial_release')=='scoped_bootstrap'
        and workflow.get('current_stage')=='release_planning' and not workflow.get('release_id')
        and not admitted_pointer(vault,workflow))


def fail(message):
    raise ContractError('PAGE_GOVERNANCE_INVALID', '$', message)


def admitted_pointer(vault, workflow):
    """Read only sealed supported-recovery authority; no mutable side channel."""
    selected = None
    for entry in workflow.get('release_recovery_history', []):
        report = pinned(vault, {'ref':entry['report_ref'], 'sha256':entry['report_sha256']})
        q = report['request']
        if (q.get('workflow_id') != workflow['workflow_id'] or q.get('actor') != workflow['actor']
                or q.get('recovery_id') != entry['recovery_id']
                or q.get('input_digest') != entry['input_digest']
                or q['input_digest'] != 'sha256:'+fingerprint({k:v for k,v in q.items() if k!='input_digest'})
                or report.get('domain_calls_granted') != 0):
            fail('initial release recovery authority changed')
        pointer = q.get('initial_page_governance')
        if pointer:
            from .page_governance import authorization
            grant = authorization(vault, pointer)
            request = grant['request']
            if (grant.get('contract') != CONTRACT or q.get('recovery_scope') not in ('governance-resolved/v1','initial-release-preflight/v1')
                    or request.get('workflow_id') != workflow['workflow_id']
                    or request.get('actor') != workflow['actor']
                    or request.get('base_release') != report['blocked_plan']
                    or request.get('source_governance_receipt') != report['governance_receipt']):
                fail('initial page authority escaped its admitted recovery')
            if selected is not None and selected != pointer:
                fail('first internal release already has a different authorization')
            selected = pointer
    return selected


def validate(service, workflow, pointer, *, blocked_plan=None, governance_receipt=None):
    """Admission and native planning both bind the same exact current inputs."""
    from .page_governance import authorization
    grant = authorization(service.vault, pointer)
    q = grant['request']
    vault_id = service.source.vault_manifest['vault']['id']
    if (grant.get('contract') != CONTRACT or q.get('basis_kind') != 'initial_draft_plan'
            or q.get('workflow_id') != workflow['workflow_id'] or q.get('actor') != workflow['actor']
            or q.get('retrieval_scope') != vault_id or q.get('human_review_performed') is not False
            or grant.get('new_model_calls') != 0
            or workflow.get('scope', {}).get('execution_mode') != 'auto_full'
            or service._state().get('current_release_id')):
        fail('exact first internal release scope required')
    if blocked_plan is not None and q.get('base_release') != blocked_plan:
        fail('initial grant names another blocked plan')
    if governance_receipt is not None and q.get('source_governance_receipt') != governance_receipt:
        fail('initial grant names another source resolution')
    bootstrap = q.get('bootstrap', {})
    if bootstrap.get('ref') != workflow['scope'].get('bootstrap_ref'):
        fail('initial grant names another Bootstrap')
    start = pinned(service.vault, bootstrap)
    if (start.get('contract') != 'vault-startup-report/v1' or start.get('vault_id') != vault_id
            or start.get('workflow_id') != workflow['workflow_id']):
        fail('Bootstrap identity differs')
    ready = {o['resource_id']:o for o in workflow['source_outcomes'] if o['status']=='ready'}
    hashes = set(start['source_hashes'].values())
    if not ready or any(o['content_sha256'] not in hashes for o in ready.values()):
        fail('ready sources escaped the Bootstrap inputs')
    base = pinned(service.vault, q['base_release'])
    validate_record('vault_finalize_plan', base)
    if (q['base_release']['ref'] != service._plan_path(base['release_id']).relative_to(service.vault).as_posix()
            or base['state'] != 'draft' or base['actor'] != workflow['actor']
            or base['blockers'] != ['no-deliverable-pages'] or base.get('page_governance')
            or base['plan_id'] != fingerprint({k:v for k,v in base.items() if k not in ('revision','state','plan_id')})
            or {r['run_id'] for r in base['build_runs']} != set(service.build._batch(workflow['batch_id'])['run_ids'])):
        fail('initial draft plan or Build scope changed')
    receipt = pinned(service.vault, q['source_governance_receipt'])
    transaction = pinned(service.vault, q['source_governance_transaction'])
    source_q = transaction['request']
    operation = source_q['operation_id']
    source_root = f'_system/metadata/source-governance-resolutions/{operation}'
    resolved = {r['resource_id'] for r in receipt['rows'] if r['resolved']}
    if (not receipt.get('applied') or not resolved or not resolved.issubset(ready)
            or receipt.get('operation_id') != operation or receipt.get('model_calls') != 0
            or q['source_governance_receipt']['ref'] != source_root+'/receipt.json'
            or q['source_governance_transaction']['ref'] != source_root+'/transaction.json'
            or source_q.get('workflow_id') != workflow['workflow_id'] or source_q.get('actor') != workflow['actor']
            or source_q.get('workflow_ref') != f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}.json"
            or set(source_q.get('resource_ids', [])) != set(ready)
            or source_q.get('automatic_approval_activation') is not True
            or source_q.get('input_digest') != 'sha256:'+fingerprint({k:v for k,v in source_q.items() if k!='input_digest'})):
        fail('source resolution escaped the exact initial workflow')
    return base
