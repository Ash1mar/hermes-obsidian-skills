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


def checkpoint_ref(workflow,folder,name):
    """New validation gets a new address; every original audit file stays intact."""
    suffix=''
    if name in ('checkpoint_1','checkpoint-1-validation'):
        pointers=[p for p in workflow.get('execution_recovery_history',[])
                  if 'checkpoint-1-validate' in p.get('nodes',[])]
        if pointers:suffix='-'+fingerprint(pointers[-1])[:24]
    return f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}/{folder}/{name}{suffix}.json"


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
        entry=report['entries'][0];proof=entry['coverage_admission']
        if found and (entry.get('supersedes_scope')!=found['ref']
                or proof.get('reused_native_authorization')!=found['ref']
                or proof['manifest_sha256']!=found['ref']['manifest_sha256']):
            fail('an active scope may only be recertified with the identical approved manifest')
        if found:
            if not entry.get('basis'):fail('recertification has no pinned cause')
            for item in entry['basis']:pinned(vault,item['ref'],item['sha256'])
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
    expected=current['proof'].get('run_tasks')
    if expected is None or 'partial_task_ids' not in current['proof']:
        fail('scope requires source-completion isolation certification before materialization')
    if any(r['tasks']!=expected for r in (request['runs'] if global_request else [request])):
        fail('run may contain only tasks supporting the approved page set')
    return scope


def check_run(service, run):
    if not run.get('coverage_scope'):return None
    current,w=resolve(service,run['coverage_scope'])
    batch=service._batch(w['batch_id'])
    if run['run_id'] not in batch['run_ids'] or run['actor']!=w['actor']:fail('run is outside the scoped batch')
    check_decisions(current,run['decisions'],persisted=True)
    if run['tasks']!=current['proof']['run_tasks']:fail('run task isolation differs')
    hashes={p['path']:p['authored_content_sha256'] for p in current['proof']['pages']}
    if len(run['page_revisions'])!=len(hashes) or any(hashes.get(p['path'])!=p['authored_sha256'] for p in run['page_revisions']):
        fail('draft bytes or downstream page set changed')
    return {'outcome':'limited_scope','coverage_complete':False,'coverage_scope':current['ref'],
            'allowed_page_count':len(hashes),'deferred_candidate_occurrences':current['manifest']['deferred_candidate_occurrences'],
            'held_page_count':current['manifest']['held_page_count']}


def partial_tasks(service,scope):
    if not scope:return set()
    current,_=resolve(service,scope);proof=current['proof'];partial=set(proof['partial_task_ids'])
    if not partial.issubset(t['task_id'] for t in proof['run_tasks']):fail('partial-task partition differs')
    if partial!={g['task_id'] for g in proof['task_coverage_gaps']}:fail('partial tasks lost their unfinished evidence')
    return partial


def batch_outcome(service,batch):
    if not batch.get('global_reduction_id'):return {}
    record=service._global_reduction(batch['batch_id'],batch['global_reduction_id'])
    scope=record.get('coverage_scope')
    if not scope:return {}
    current,w=resolve(service,scope)
    if w['batch_id']!=batch['batch_id'] or record['omitted_candidate_refs']!=current['proof']['omitted_candidate_refs']:
        fail('global gap accounting differs')
    proof=current['proof']
    if set(batch['task_ids'])!={t['task_id'] for t in proof['run_tasks']}|set(proof['outside_page_scope_task_ids']):
        fail('batch task partition differs')
    if batch['run_ids']!=record['run_ids']:fail('batch run set differs')
    return {'outcome':'limited_scope','coverage_complete':False,'coverage_scope':scope,
            'allowed_page_count':len(current['proof']['pages']),
            'held_page_count':current['manifest']['held_page_count'],
            'deferred_candidate_occurrences':current['manifest']['deferred_candidate_occurrences'],
            'outside_page_scope_task_ids':proof['outside_page_scope_task_ids'],
            'outside_scope_required_inspection_gaps':proof['outside_scope_required_inspection_gaps'],
            'partial_task_ids':proof['partial_task_ids'],'task_coverage_gaps':proof['task_coverage_gaps']}


def task_partition(service,tasks,pages):
    """Only actual adopted citation ownership may assign work to a scoped run."""
    with service.source.reading_snapshot():
        return _task_partition(service,tasks,pages)


def _task_partition(service,tasks,pages):
    from .reading_dependencies import required_reading,citation_dependency_gaps
    from .validation import canonical_json,validate_references
    from .knowledge_build import _overlaps
    candidates,records=service._pass_index(t['task_id'] for t in tasks)
    used=set();used_passes={};supports={}
    for page in pages:
        for ref in page['candidate_refs']:
            match=candidates.get((ref['pass_id'],ref['candidate_id']))
            if not match or match[0]['pass_kind']!='citation':fail('scoped support is not a current citation')
            record,candidate=match;used.add(record['task_id']);used_passes[record['pass_id']]=record
            supports.setdefault(record['task_id'],[]).extend(candidate['support_refs'])
    task_records={t['task_id']:service._task(t['task_id']) for t in tasks}
    def pass_refs(record):
        return [i['source_ref'] for i in record['inspections']]+[r for c in record['candidates'] for r in c['support_refs']]
    def task_refs(task):
        return [*task['target_refs'],*[i['source_ref'] for i in task['inspections']],*[i['source_ref'] for i in task['deferred']]]
    refs=[r for p in used_passes.values() for r in pass_refs(p)]
    refs.extend(r for t in task_records.values() for r in task_refs(t))
    actor=next(iter(task_records.values()))['actor']
    index={canonical_json(u['ref']):u for u in service._live_units(refs,actor,service._document_registry_revision())}
    def units_for(refs):
        return [index[k] for k in dict.fromkeys(canonical_json(r['unit_ref']) for r in refs)]
    for record in used_passes.values():validate_references('knowledge_pass',record,units_for(pass_refs(record)))
    selected=[];outside=[];gaps=[];partial=[];coverage_gaps=[]
    for snapshot in tasks:
        tid=snapshot['task_id'];task=task_records[tid]
        if task['revision']!=snapshot['revision']:fail('task revision changed during isolation')
        package=service._existing_reading_package(task,task['actor'],service._document_registry_revision())
        missing=[]
        if package:
            _,supplement=required_reading(service.source,package)
            missing=citation_dependency_gaps(service.source,package,records[tid][-1],supplement)
        if tid in used:
            if missing or any(_overlaps(d['source_ref'],r) for d in task['deferred'] for r in supports[tid]):
                fail('an allowed page depends on unfinished required inspection')
            probe={**task,'status':'completed','deferred':[],
                'outputs':['_system/knowledge-builds/pending.md'],'reason':'scope completion probe'}
            error=None
            try:validate_references('work',probe,units_for(task_refs(task)))
            except ContractError as exc:
                if exc.code not in ('INCOMPLETE_COVERAGE','INVALID_COMPLETION'):raise
                error={'code':exc.code,'reason':str(exc)}
            if error or task['deferred']:
                partial.append(tid);coverage_gaps.append({'task_id':tid,'completion_error':error,
                    'target_refs':task['target_refs'],'inspections':task['inspections'],'deferred':task['deferred'],
                    'completed':False})
            selected.append(snapshot)
        else:
            outside.append(tid);gaps.extend(missing)
    return {'run_tasks':selected,'outside_page_scope_task_ids':outside,
            'outside_scope_required_inspection_gaps':gaps,'partial_task_ids':partial,
            'task_coverage_gaps':coverage_gaps,'adopted_citation_passes_checked':len(used_passes),
            'citation_support_failures':0}
