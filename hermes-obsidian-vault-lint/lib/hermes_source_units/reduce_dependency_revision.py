"""Narrow extension of citation revision for unconsumed, cancelled Reduce evidence."""
import hashlib
import json

from .source_units import _load_json, _vault_path
from .validation import ContractError, fingerprint
from .reading_dependencies import citation_dependency_gaps

SCOPE='reduce-dependencies/v1'


def scoped(value):
    return value.get('pass_revision',{}).get('revision_scope')==SCOPE


def snapshot(service,value):
    """Immutable domain evidence, excluding mutable batch/lease control records."""
    files={}
    def pin(path):
        files[path.relative_to(service.vault).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    batch=service.knowledge._batch(value['batch_id'])
    for tid in batch['task_ids']:
        pin(service.knowledge._task_path(tid))
        root=service.vault/f'_system/knowledge-builds/task-{tid}'
        for kind in ('passes','readings'):
            for path in (root/kind).glob('*.json'):pin(path)
    for outcome in value['source_outcomes']:
        for ref in (outcome['path'],*outcome['artifact_refs']):pin(_vault_path(service.vault,ref))
    for template in value['template_pins']:pin(_vault_path(service.vault,template['path']))
    return {'workflow_id':value['workflow_id'],'batch_id':value['batch_id'],'scope':value['scope'],
            'pinned_inputs':value['pinned_inputs'],'source_outcomes':value['source_outcomes'],
            'dispatch_policy':value['dispatch_policy'],'files':files}


def authorize(service,value,request):
    def fail(code,message):raise ContractError(code,'$',message)
    if (value['state']!='cancelled' or not value['cancel_requested'] or value['current_stage']!='reducing'
            or service.pending_pause(value) or value['dispatch_policy']['mode']!='full'
            or request.get('evidence_digest')!='sha256:'+fingerprint(value['pause_control'])):
        fail('INVALID_TRANSITION','dependency revision requires cancelled released Reduce and unchanged pause history')
    batch=service.knowledge._batch(value['batch_id'])
    if not batch['cancel_requested'] or batch['state']!='cancelled':fail('WORKER_ACTIVE','batch must be cancelled')
    service.assert_execution_ready(value);service._verify_execution_evidence(value)
    selected={item['task_id'] for item in request.get('tasks',[])}
    if not selected or not selected.issubset(set(batch['task_ids'])-service.skipped_pass_tasks(value)):
        fail('INVALID_SCOPE','select only existing non-skipped citation tasks')
    failures=request.get('reduce_failures',[])
    resources=set()
    for item in failures:
        node=item.get('node','')
        card=next((c for c in value['kanban']['task_map'] if c['node']==node),None)
        path=_vault_path(service.vault,f"_system/ledgers/ingest-workflows/{value['workflow_id']}/reports/failed-{fingerprint(node)[:16]}.json")
        data=path.read_bytes();report=json.loads(data)
        if (not node.startswith('resource-reduce:') or not card or card['task_id']!=item.get('task_id')
                or item.get('failure_sha256')!=hashlib.sha256(data).hexdigest()
                or report.get('task_id')!=card['task_id'] or report.get('idempotency_key')!=card['idempotency_key']
                or report.get('error_code')!='INCOMPLETE_COVERAGE'
                or 'required original dependency lacks citation inspection' not in report.get('reason','')):
            fail('STALE_INPUT','dependency revision requires the current bound inspection failure')
        resources.add(node.partition(':')[2])
    for item in request['tasks']:
        task=service.knowledge._task(item['task_id'])
        if {r['unit_ref']['resource_id'] for r in task['target_refs']}-resources:
            fail('INVALID_SCOPE','task must belong to an evidenced failed resource')
        service.knowledge._verify_planned_measurement(task,batch['actor'],batch['document_registry_revision'],None)
        records=service.knowledge._pass_index([item['task_id']])[1][item['task_id']]
        prior=max(records,key=lambda p:p['sequence'])
        if (prior['pass_id']!=item['pass_id'] or prior['pass_kind']!='citation'
                or fingerprint({k:v for k,v in prior.items() if k!='pass_id'})!=prior['pass_id']):
            fail('STALE_INPUT','dependency revision must pin the current valid citation')
        package=_load_json(_vault_path(service.vault,
            f"_system/knowledge-builds/task-{item['task_id']}/readings/{prior['reading_package_id']}.json"))
        if not citation_dependency_gaps(service.knowledge.source,package,prior):
            fail('INVALID_SCOPE','unaffected citation cannot be revised through dependency recovery')
    return snapshot(service,value)
