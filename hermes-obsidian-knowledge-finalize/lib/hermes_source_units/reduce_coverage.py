"""Explicit incomplete coverage, bound to a trusted immutable admission report.

Omission is accounting only. A limited result never establishes whole-batch
coverage, and its exact page whitelist follows it through validation/finalize.
"""
import hashlib
import json
from .source_units import _vault_path, _load_json
from .validation import ContractError, fingerprint

ADMIT='reduce-coverage/v1'
REOPEN='reduce-coverage-reopen/v1'


def fail(message):
    raise ContractError('COVERAGE_SCOPE_INVALID','$',message)


def pinned(vault, ref, digest):
    data=_vault_path(vault,ref).read_bytes()
    if hashlib.sha256(data).hexdigest()!=digest:fail('pinned coverage evidence changed')
    return json.loads(data)


def active(vault, workflow):
    found=None
    for pointer in workflow.get('execution_recovery_history',[]):
        report=pinned(vault,pointer['report_ref'],pointer['report_sha256'])
        request=report['request'];scope=request.get('recovery_scope')
        if scope not in (ADMIT,REOPEN):continue
        if (request['workflow_id']!=workflow['workflow_id'] or request['actor']!=workflow['actor']
                or report['batch_id']!=workflow['batch_id']
                or request['input_digest']!=pointer['input_digest']):fail('coverage authority differs')
        if scope==REOPEN:
            if not found or request['coverage_scope']!=found['ref']:fail('withdrawal has no matching scope')
            found=None;continue
        if found:fail('withdraw the existing limited scope before admitting another')
        entry=report['entries'][0];proof=entry['coverage_admission']
        manifest=pinned(vault,proof['manifest_ref'],proof['manifest_sha256'])
        approval=pinned(vault,proof['approval_ref'],proof['approval_sha256'])
        if (manifest['workflow_id']!=workflow['workflow_id'] or not approval.get('accept_necessary_coverage_gaps')
                or approval['actor']!=workflow['actor'] or approval['manifest_sha256']!=proof['manifest_sha256']
                or proof['new_model_calls_granted']!=0):fail('limited scope lacks exact approval')
        ref={'contract':ADMIT,'workflow_id':workflow['workflow_id'],
             'authorization_ref':pointer['report_ref'],'authorization_sha256':pointer['report_sha256'],
             'manifest_ref':proof['manifest_ref'],'manifest_sha256':proof['manifest_sha256']}
        found={'ref':ref,'manifest':manifest,'proof':proof}
    return found


def resolve(service, scope):
    from .ingest_workflow import FileIngestWorkflowService
    workflow=FileIngestWorkflowService(service.vault).status(scope['workflow_id'])
    current=active(service.vault,workflow)
    if not current or current['ref']!=scope:fail('scope is stale, withdrawn or foreign')
    return current,workflow


def key(identity):
    return identity['kind'],identity['identity_key']


def check_decisions(current, decisions, *, persisted=False):
    allowed=current['proof']['pages']
    if len(decisions)!=len(allowed):fail('page whitelist count differs')
    rows={p['path']:p for p in allowed}
    if len(rows)!=len(allowed) or {d['path'] for d in decisions}!=set(rows):fail('page whitelist differs')
    for d in decisions:
        p=rows[d['path']]
        if key(d['identity'])!=key(p['identity']) or d['candidate_refs']!=p['candidate_refs']:
            fail('page identity or candidate ownership differs')
        if not persisted and hashlib.sha256(d['content'].encode('utf-8')).hexdigest()!=p['submitted_content_sha256']:
            fail('scope content bytes differ')


def check_request(service, request, *, global_request=False):
    scope=request.get('coverage_scope')
    from .workflow_guard import _BINDING,_ACTIVE
    authority=_BINDING.get() or _ACTIVE.get()
    if authority:
        from .ingest_workflow import FileIngestWorkflowService
        workflow=FileIngestWorkflowService(service.vault).status(authority['workflow_id'])
        current=active(service.vault,workflow)
        if current and scope!=current['ref']:fail('scoped worker cannot submit an unscoped result')
    if scope is None:return None
    if not authority or authority['workflow_id']!=scope['workflow_id'] or authority.get('node',authority.get('kind'))!='global-reduce':
        fail('scope materialization requires its bound global native worker')
    current,w=resolve(service,scope)
    if request.get('actor')!=w['actor'] or (global_request and request['batch_id']!=w['batch_id']):
        fail('scope actor or batch differs')
    if global_request:
        if len(request['runs'])!=1:fail('scope materializes exactly one isolated run')
        if request['omitted_candidate_refs']!=current['proof']['omitted_candidate_refs']:
            fail('necessary gaps must remain exactly accounted and incomplete')
        for run in request['runs']:
            if run.get('coverage_scope')!=scope:fail('draft run lost coverage scope')
            check_decisions(current,run['decisions'])
    else:check_decisions(current,request['decisions'])
    return scope


def check_run(service, run):
    if not run.get('coverage_scope'):return None
    current,w=resolve(service,run['coverage_scope'])
    batch=service._batch(w['batch_id'])
    if run['run_id'] not in batch['run_ids'] or run['actor']!=w['actor']:fail('run is outside the scoped batch')
    check_decisions(current,run['decisions'],persisted=True)
    hashes={p['path']:p['authored_content_sha256'] for p in current['proof']['pages']}
    if len(run['page_revisions'])!=len(hashes) or any(hashes.get(p['path'])!=p['authored_sha256'] for p in run['page_revisions']):
        fail('draft bytes or downstream page set changed')
    return {'outcome':'limited_scope','coverage_complete':False,'coverage_scope':current['ref'],
            'allowed_page_count':len(hashes),'deferred_candidate_occurrences':current['manifest']['deferred_candidate_occurrences'],
            'held_page_count':current['manifest']['held_page_count']}


def batch_outcome(service,batch):
    if not batch.get('global_reduction_id'):return {}
    record=service._global_reduction(batch['batch_id'],batch['global_reduction_id'])
    scope=record.get('coverage_scope')
    if not scope:return {}
    current,w=resolve(service,scope)
    if w['batch_id']!=batch['batch_id'] or record['omitted_candidate_refs']!=current['proof']['omitted_candidate_refs']:
        fail('global gap accounting differs')
    return {'outcome':'limited_scope','coverage_complete':False,'coverage_scope':scope,
            'allowed_page_count':len(current['proof']['pages']),
            'held_page_count':current['manifest']['held_page_count'],
            'deferred_candidate_occurrences':current['manifest']['deferred_candidate_occurrences']}
