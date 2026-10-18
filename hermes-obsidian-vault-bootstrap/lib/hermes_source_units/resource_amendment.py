"""Read the explicit, immutable authorization for an unconsumed source revision."""
import hashlib
from .source_units import _load_json, _vault_path
from .validation import ContractError

SCOPE='source-result-review/v1'


def entry(service,workflow,node):
    for pointer in reversed(workflow.get('execution_recovery_history',[])):
        if node not in pointer['nodes']:continue
        path=_vault_path(service.vault,pointer['report_ref'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=pointer['report_sha256']:
            raise ContractError('STALE_INPUT','$','source review authorization changed')
        report=_load_json(path)
        if report['request'].get('recovery_scope')!=SCOPE:continue
        found=next(e for e in report['entries'] if e['node']==node)
        return {**found,'authorization_sha256':pointer['report_sha256']}
    return None


def pending(service,workflow,node):
    grant=entry(service,workflow,node)
    if grant and grant['replaces_reduction_id'] in service.knowledge._batch(workflow['batch_id'])['resource_reduction_ids']:
        return grant
    return None


def authorize(knowledge,resource_id,old_id):
    from .workflow_guard import _ACTIVE
    from .ingest_workflow import FileIngestWorkflowService
    context=_ACTIVE.get()
    if not context or context['kind']!='resource-reduce':
        raise ContractError('ACCESS_DENIED','$','source replacement needs its current bound worker')
    service=FileIngestWorkflowService(knowledge.vault);workflow=service.status(context['workflow_id'])
    grant=entry(service,workflow,'resource-reduce:'+resource_id)
    batch=knowledge._batch(workflow['batch_id'])
    if (not grant or grant['replaces_reduction_id']!=old_id or batch['global_reduction_id'] or batch['run_ids']
            or hashlib.sha256(knowledge._resource_reduction_path(batch['batch_id'],old_id).read_bytes()).hexdigest()!=grant['reduction_sha256']):
        raise ContractError('RECOVERY_NOT_ALLOWED','$','source replacement differs from its unconsumed reviewed result')
    return {'reduction_id':old_id,'authorization_sha256':grant['authorization_sha256'],
            'selected_candidate_refs':grant['selected_candidate_refs']}
