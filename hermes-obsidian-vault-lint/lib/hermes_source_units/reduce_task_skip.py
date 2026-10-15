"""Explicitly defer unconsumed QA-limited tasks using the existing coverage-gap ledger."""
import copy
import hashlib
import json
import os
import re

from .source_units import _exclusive_lock,_load_json,_vault_path,_write_atomic,_json_bytes
from .validation import ContractError,fingerprint,validate_record
from .reduce_dependency_revision import assert_unconsumed_resources


def fail(code,message):raise ContractError(code,'$',message)


def verify(service,value,report,pointer):
    if pointer['slice_ids'] or report['task_ids']!=pointer['task_ids']:
        fail('STALE_INPUT','Reduce deferral must preserve slice ownership')
    for entry in report['entries']:
        task=service.knowledge._task(entry['task_id'])
        if task['status']!='skipped' or fingerprint(task['target_refs'])!=entry['target_fingerprint']:
            fail('STALE_INPUT','deferred task status or coverage changed')
    for ref,digest in report['protected_files'].items():
        if hashlib.sha256(_vault_path(service.vault,ref).read_bytes()).hexdigest()!=digest:
            fail('SOURCE_CHANGED','deferred original evidence changed')


def skip(service,request,*,dry_run=False):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('ACCESS_DENIED','Reduce deferral requires a trusted operator')
    service._check_request(request);wid=request['workflow_id'];op=request.get('operation_id','')
    if not isinstance(op,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',op):fail('INVALID_SCHEMA','invalid deferral ID')
    entries=request.get('entries',[])
    if not entries or len({e['task_id'] for e in entries})!=len(entries):fail('INVALID_SCHEMA','select unique QA-limited tasks')
    with _exclusive_lock(service._lock(wid)):
        value=service.status(wid)
        if value['actor']!=request['actor']:fail('ACTOR_MISMATCH','deferral actor differs')
        prior=next((p for p in value.get('pass_skip_history',[]) if p['operation_id']==op),None)
        journal=service._pass_skip_journal(wid)
        if prior:
            report=_load_json(_vault_path(service.vault,prior['report_ref']))
            if report['request']!=dict(request):fail('IDEMPOTENCY_CONFLICT','deferral ID already used')
            if journal.exists():
                pending=_load_json(journal)
                if pending['request']!=dict(request):fail('IDEMPOTENCY_CONFLICT','finish pending deferral')
                if not dry_run:journal.unlink()
            if not journal.exists():service.verify_pass_skips(value)
            return {'ok':True,'applied':False,'replayed':True,'resumed':False,'report_ref':prior['report_ref']}
        value=service._mutation(request)
        if (value['state']!='cancelled' or not value['cancel_requested'] or value['current_stage']!='reducing'
                or service.pending_pause(value) or value['dispatch_policy']['mode']!='full'):
            fail('INVALID_TRANSITION','deferral requires cancelled released Reduce')
        if value.get('pass_revision') and value['pass_revision']['state']!='accepted':fail('REVISION_REVIEW_REQUIRED','finish citation review first')
        k=service.knowledge
        with _exclusive_lock(k._batch_lock_path(value['batch_id'])):
            batch=k._batch(value['batch_id'])
            if not batch['cancel_requested'] or any(s['lease']['worker_id'] for s in k._slices(batch['batch_id'])):
                fail('WORKER_ACTIVE','deferral requires cancelled batch without leases')
            assert_unconsumed_resources(service,value,[e['task_id'] for e in entries])
            if journal.exists():
                pending=_load_json(journal)
                if pending['request']!=dict(request):fail('IDEMPOTENCY_CONFLICT','finish same pending deferral')
                report,updates=pending['report'],pending['updates']
            else:
                service.assert_execution_ready(value);service._verify_execution_evidence(value)
                audited=[];updates=[];protected={}
                def pin(ref):protected[ref]=hashlib.sha256(_vault_path(service.vault,ref).read_bytes()).hexdigest()
                refs=request.get('evidence_refs',[])
                if not refs or not request.get('reason','').strip():fail('ARTIFACT_REQUIRED','deferral needs persisted reviewer evidence and reason')
                for ref in refs:pin(ref)
                for e in entries:
                    tid=e['task_id']
                    if tid not in batch['task_ids'] or tid in service.skipped_pass_tasks(value) or not e.get('reason','').strip():fail('INVALID_SCOPE','select existing unskipped tasks with reasons')
                    task=k._task(tid)
                    if task['actor']!=value['actor'] or task['status']!='running':fail('INVALID_SCOPE','only unconsumed owned tasks may defer')
                    k._verify_planned_measurement(task,value['actor'],batch['document_registry_revision'],None)
                    records=k._pass_index([tid])[1].get(tid,[]);latest=max(records,key=lambda r:r['sequence'])
                    if latest['pass_id']!=e.get('pass_id') or latest['pass_kind']!='citation':fail('STALE_INPUT','pin the latest citation')
                    if fingerprint({f:v for f,v in latest.items() if f!='pass_id'})!=latest['pass_id']:fail('SOURCE_CHANGED','citation fingerprint changed')
                    qa=[i for i in latest['inspections'] if i['qa']=='needs-qa']
                    if not qa:fail('SKIP_NOT_ALLOWED','only recorded unresolved original QA may defer; repair program/dependency/transport faults')
                    package_ref=f'_system/knowledge-builds/task-{tid}/readings/{latest["reading_package_id"]}.json'
                    package=_load_json(_vault_path(service.vault,package_ref))
                    k._reading_package_fingerprint(package,latest['reading_package_id'])
                    from .reading_dependencies import citation_dependency_gaps
                    if citation_dependency_gaps(k.source,package,latest):
                        fail('SKIP_NOT_ALLOWED','required dependency inspection gaps require repair before QA deferral')
                    failure_ref=e['failure_ref'];path=_vault_path(service.vault,failure_ref)
                    failure=_load_json(path);node=failure.get('node','');card=next((c for c in value['kanban']['task_map'] if c['node']==node),None)
                    resources={r['unit_ref']['resource_id'] for r in task['target_refs']}
                    if (path!=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(node)[:16]}.json')
                            or not node.startswith('resource-reduce:') or resources!={node.partition(':')[2]} or not card
                            or failure.get('task_id')!=card['task_id'] or failure.get('idempotency_key')!=card['idempotency_key']
                            or failure.get('error_code')!='SEMANTIC_REVIEW_REQUIRED'
                            or hashlib.sha256(path.read_bytes()).hexdigest()!=e['failure_sha256']):fail('STALE_INPUT','pin the current source semantic hold')
                    for p in (service.vault/f'_system/knowledge-builds/task-{tid}/passes').glob('*.json'):pin(p.relative_to(service.vault).as_posix())
                    pin(package_ref)
                    after=copy.deepcopy(task);after.update(status='skipped',reason=e['reason'],revision=task['revision']+1)
                    validate_record('work',after)
                    updates.append({'ref':k._task_path(tid).relative_to(service.vault).as_posix(),
                        'before_sha256':hashlib.sha256(k._task_path(tid).read_bytes()).hexdigest(),'after':after})
                    audited.append({'task_id':tid,'target_fingerprint':fingerprint(task['target_refs']),'target_refs':task['target_refs'],
                        'pass_id':latest['pass_id'],'withheld_candidate_ids':[c['candidate_id'] for c in latest['candidates']],
                        'qa':qa,'reason':e['reason'],'failure_ref':failure_ref,'failure_sha256':e['failure_sha256'],'failure':failure})
                report={'contract':'reduce-task-coverage-gaps/v1','request':dict(request),'batch_id':batch['batch_id'],
                    'entries':audited,'task_ids':[e['task_id'] for e in audited],'protected_files':protected,
                    'verification_success':False,'domain_outputs_preserved':True,'permanently_unavailable':False}
            if dry_run:return {'ok':True,'applied':False,'resumed':False,'coverage_gaps':report['entries']}
            if not journal.exists():_write_atomic(journal,_json_bytes({'request':dict(request),'report':report,'updates':updates}))
            for update in updates:
                path=_vault_path(service.vault,update['ref']);data=path.read_bytes();after=_json_bytes(update['after'])
                if data==after:continue
                if hashlib.sha256(data).hexdigest()!=update['before_sha256']:fail('STALE_INPUT','task changed during deferral')
                _write_atomic(path,after)
            ref=f'_system/ledgers/ingest-workflows/{wid}/pass-skips/{op}.json';data=_json_bytes(report)
            path=_vault_path(service.vault,ref)
            if path.exists() and path.read_bytes()!=data:fail('IDEMPOTENCY_CONFLICT','deferral evidence differs')
            _write_atomic(path,data)
            value.setdefault('pass_skip_history',[]).append({'operation_id':op,'input_digest':request['input_digest'],
                'report_ref':ref,'report_sha256':hashlib.sha256(data).hexdigest(),'slice_ids':[],'task_ids':report['task_ids']})
            value['revision']+=1;service._write(value);journal.unlink()
            return {'ok':True,'applied':True,'revision':value['revision'],'report_ref':ref,'resumed':False,'state':'cancelled','skipped_tasks':len(report['task_ids'])}
