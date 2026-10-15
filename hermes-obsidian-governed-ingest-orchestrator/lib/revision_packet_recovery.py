"""Authenticate a failed revision's observed bounded packet before one corrected retry."""
import ast
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path
from hermes_source_units.validation import fingerprint
from native_pass_identity import load_context

SCOPE='revision-citation-packet/v1'
CAUSES={
    'ACCESS_DENIED':'ACCESS_DENIED at $: ACCESS_DENIED at $: typed submission requires a bounded model slice',
    'STALE_INPUT':'STALE_INPUT at $: STALE_INPUT at $: reviewed candidate identity changed'}


def inspection_slots(report):
    prefix='PASS_EXECUTION_BLOCKED at $: '
    if report.get('error_code')!='PASS_EXECUTION_BLOCKED' or not report.get('reason','').startswith(prefix):return []
    try:failures=ast.literal_eval(report['reason'][len(prefix):])
    except (ValueError,SyntaxError):return []
    if (not isinstance(failures,list) or not failures or any(not isinstance(f,dict)
            or f.get('stage')!='pass' or f.get('code')!='INCOMPLETE_COVERAGE'
            or f.get('message')!='INCOMPLETE_COVERAGE at $: required reading dependency has not been inspected; retain original evidence'
            or not isinstance(f.get('task'),str) for f in failures)):return []
    return [f['task'] for f in failures]


def interrupted_codes(report,snapshot):
    """A generic transport refusal needs an actual ended native cancellation."""
    from ingest_kanban import mechanical_failure_codes
    if report.get('error_code')!='PROGRAM_EXECUTION_FAILED':
        return mechanical_failure_codes(report)
    attempt=next((r for r in reversed(snapshot.get('runs',[])) if r.get('worker_pid')),None)
    if (report.get('reason')!='native model request did not complete; inspect its session'
            or not attempt or attempt.get('summary')!='Vault workflow cancelled'
            or attempt.get('status')!='scheduled' or attempt.get('outcome')!='scheduled'
            or not attempt.get('ended_at') or not attempt.get('started_at')
            or attempt['ended_at']<attempt['started_at']
            or snapshot.get('task',{}).get('completed_at') or snapshot.get('task',{}).get('result')):
        raise ContractError('RECOVERY_NOT_ALLOWED','$','transport failure lacks ended native cancellation evidence')
    return ['WORKFLOW_STOPPED']


def unspent_correction(adapter,workflow,binding,report,identity):
    slots=inspection_slots(report)
    if not slots:return identity
    audits=[]
    root=adapter.workflow.vault/'_system/ledgers/ingest-workflows'/workflow['workflow_id']/'semantic-calls'
    for path in root.glob('*.json'):
        call=_load_json(path)
        if call.get('native_task_id')==binding['task_id'] and call.get('phase')=='citation' and not call.get('reused_from_audit'):
            audits.append((path,call))
    if len(audits)!=1 or audits[0][1].get('semantic_attempt_limit')!=2:
        raise ContractError('RECOVERY_NOT_ALLOWED','$','requires exactly one native call with its original correction unused')
    path,call=audits[0]
    tids=[call.get('task_identities',{}).get(slot) for slot in slots]
    if not tids or any(t not in identity['pending_task_ids'] for t in tids):
        raise ContractError('STALE_INPUT','$','failed inspection slots are not pending authorized tasks')
    return {**identity,'remaining_correction_task_ids':tids,
            'original_call_ref':path.relative_to(adapter.workflow.vault).as_posix(),
            'remaining_semantic_attempts':1}


def verify(adapter,workflow,item,binding,*,partial=False):
    revision=workflow['pass_revision'];service=adapter.workflow.knowledge
    targets={t['task_id']:t for t in revision['tasks']}
    owned=[tid for tid in item['task_ids'] if tid in targets]
    checked={'batch_id':workflow['batch_id'],'task_ids':owned}
    _,context=load_context(adapter.workflow.vault,{**binding,'worker_id':'ingest-worker-'+binding['task_id']},checked)
    packet=_load_json(_vault_path(adapter.workflow.vault,context['packet_ref']))
    projections=[];pending=[];batch=service._batch(workflow['batch_id'])
    for index,tid in enumerate(owned,1):
        records=service._pass_index([tid])[1][tid]
        latest=max(records,key=lambda p:p['sequence'])
        if partial and latest.get('revision_id')==revision['revision_id'] and latest.get('supersedes_pass_id')==targets[tid]['pass_id'] and latest['sequence']==targets[tid]['sequence']:
            continue
        if latest['pass_id']!=targets[tid]['pass_id']:
            raise ContractError('STALE_INPUT','$','revision predecessor changed or already has a new result')
        package=service._existing_reading_package(service._task(tid),workflow['actor'],batch['document_registry_revision'])
        projection=adapter.workflow.revision_projection(workflow,tid,service.model_input(tid,package))
        candidate=projection['view']['continuation'].get('candidate',{})
        if (not candidate.get('revision_review') or candidate.get('candidate_pass_id')!=targets[tid]['pass_id']
                or context['candidates'].get(f't{index}')!=targets[tid]['pass_id']):
            raise ContractError('STALE_INPUT','$','failed input is not the authorized authored citation revision')
        projections.append(projection);pending.append((tid,index))
    current=service.model_packet(projections,workflow['batch_id'],persist=False)['view']
    if partial:
        from fixed_semantic_recovery import task_content
        for current_task,(_,index) in zip(current['tasks'],pending):
            original=next(t for t in packet['tasks'] if t['task']==f't{index}')
            if task_content(current,current_task)!=task_content(packet,original):
                raise ContractError('STALE_INPUT','$','pending revision evidence differs from the observed failed packet')
    elif fingerprint(current)!=fingerprint(packet):
        raise ContractError('STALE_INPUT','$','current required revision evidence differs from the observed failed packet')
    return {'packet_ref':context['packet_ref'],'packet_sha256':context['packet_sha256'],
            'revision_id':revision['revision_id'],'predecessors':context['candidates'],
            'template_sha256':revision['template_sha256'],'task_ids':owned,'pending_task_ids':[t for t,_ in pending]}
