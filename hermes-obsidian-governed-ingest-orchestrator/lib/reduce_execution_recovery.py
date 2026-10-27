"""One explicit native retry after a cancelled Reduce attempt; domain inputs stay intact."""
import hashlib
import json
import os
import re

from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import fingerprint, record_validation_scope
from hermes_source_units.verified_json import workflow_read_scope


def fail(code, message):
    raise ContractError(code, '$', message)


def cancelled_projection_binding(adapter,value,node,card,report,binding,path,current):
    """Authenticate a never-executed cancelled card displaced by graph narrowing."""
    if (report.get('reason')!='canonical dispatcher binding differs; trusted repair required'
            or binding.get('task_id')==card['task_id'] or current.get('runs')
            or current['task'].get('status') not in ('archived','blocked','scheduled')
            or current['task'].get('worker_pid') or current['task'].get('current_run_id')):
        fail('RECOVERY_NOT_ALLOWED','projection recovery requires an unattempted replacement')
    expected={'workflow_id':value['workflow_id'],'node':node.name,'actor':value['actor'],
        'task_id':binding.get('task_id'),'input_fingerprint':node.input_fingerprint,
        'template_hash':next(p['template_hash'] for p in value['template_pins'] if p['kind']==node.kind)}
    if binding!=expected or not isinstance(binding.get('task_id'),str):
        fail('STALE_INPUT','original canonical binding differs')
    original=adapter.kanban.task_snapshot(value['kanban']['board_id'],binding['task_id'])
    task=original.get('task',{});runs=original.get('runs',[])
    body=json.loads(task.get('body','{}'))
    if (task.get('id')!=binding['task_id'] or task.get('status')!='archived'
            or task.get('created_by')!='ingest-workflow' or task.get('worker_pid') or task.get('current_run_id')
            or any(body.get(k)!=v for k,v in {'workflow_id':value['workflow_id'],'node':node.name,
                'vault':str(adapter.workflow.vault),'input_fingerprint':node.input_fingerprint,
                'worker_binding':str(path),'template_hash':expected['template_hash'],'executor':'semantic-v1'}.items())):
        fail('STALE_INPUT','archived canonical native identity differs')
    if not runs or any(r.get('profile')!='ingest-program' or r.get('outcome')!='scheduled'
            or r.get('summary')!='Vault workflow cancelled' or r.get('worker_pid')
            or not r.get('ended_at') or r.get('started_at')!=r['ended_at'] for r in runs):
        fail('RECOVERY_NOT_ALLOWED','original card must have only zero-execution cancellation receipts')
    return original


@workflow_read_scope
def recover_reduce_execution(adapter, request, *, dry_run=False):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('ACCESS_DENIED', 'Reduce recovery requires a trusted operator')
    service = adapter.workflow
    service._check_request(request)
    wid, rid = request['workflow_id'], request.get('recovery_id', '')
    if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', rid):
        fail('INVALID_SCHEMA', 'invalid recovery ID')
    scope=request.get('recovery_scope')
    if scope == 'reduce-citation-revision/v1':
        from citation_amendment_control import recover
        return recover(adapter,request,dry_run=dry_run)
    if scope == 'reduce-citation-impact/v1':
        from citation_revision_preview import inspect
        return inspect(adapter, request, dry_run=dry_run)
    if scope not in ('build-native/v1','reduce-cancel/v1','source-packets/v1','source-hold/v1','source-hold/v2','source-hold/v3','source-format/v1','dependency-resolved/v1','source-result-review/v1','semantic-output/v1','reduce-disposition/v1','reduce-coverage/v1','reduce-coverage-reopen/v1','reduce-validation/v1'):
        fail('INVALID_SCHEMA', 'Reduce recovery requires a supported explicit correction scope')
    if scope in ('reduce-coverage/v1','reduce-coverage-reopen/v1','reduce-validation/v1'):
        from reduce_coverage import contract_hash
        if request.get('target_contract_sha256')!=contract_hash():fail('STALE_INPUT','coverage contract changed')
        if scope=='reduce-coverage-reopen/v1':
            from reduce_coverage_reopen import recover
            return recover(adapter,request,dry_run=dry_run)
    if scope=='reduce-disposition/v1':
        from reduce_disposition import contract_hash
        if request.get('target_contract_sha256')!=contract_hash():fail('STALE_INPUT','Reduce disposition contract changed')
    if scope=='semantic-output/v1':
        from semantic_output_recovery import contract_hash
        if request.get('target_contract_sha256')!=contract_hash():fail('STALE_INPUT','shared output-correction contract changed')
    review=scope=='source-result-review/v1'
    if review:
        from source_result_review import contract_hash
        if request.get('target_contract_sha256')!=contract_hash():fail('STALE_INPUT','source review contract changed')
    if scope in ('source-format/v1','dependency-resolved/v1'):
        from reduce_target_recovery import contract_hash
        if request.get('target_contract_sha256')!=contract_hash():
            fail('STALE_INPUT','target recovery implementation differs')
    if scope=='source-packets/v1':
        from fixed_reduce_packets import contract_hash
        if request.get('target_contract_sha256')!=contract_hash():
            fail('STALE_INPUT','source packet implementation differs from the reviewed request')
    if scope in ('source-hold/v1','source-hold/v2','source-hold/v3'):
        from source_hold_recovery import contract_hash
        if request.get('target_contract_sha256')!=contract_hash():
            fail('STALE_INPUT','source hold recovery contract differs')
    if not isinstance(request.get('reason'), str) or not request['reason'].strip():
        fail('INVALID_SCHEMA', 'describe the authorized cancellation recovery')
    entries = request.get('entries')
    if (not isinstance(entries, list) or not entries or any(not isinstance(e, dict) for e in entries)
            or len({e.get('node') for e in entries}) != len(entries)):
        fail('INVALID_SCHEMA', 'select unique interrupted Reduce nodes')
    lock = _vault_path(service.vault, f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock')
    with _exclusive_lock(lock), _exclusive_lock(service._lock(wid)), record_validation_scope():
        value = service.status(wid)
        if request['actor'] != value['actor']:
            fail('ACTOR_MISMATCH', 'recovery actor differs')
        service.assert_execution_ready(value)
        prior = next((r for r in value.get('execution_recovery_history', []) if r['recovery_id'] == rid), None)
        if prior:
            if prior['input_digest'] != request['input_digest']:
                fail('IDEMPOTENCY_CONFLICT', 'recovery ID already used')
            return {'ok': True, 'replayed': True, 'applied': False, 'workflow_id': wid,
                    'revision': value['revision'], 'report_ref': prior['report_ref'], 'nodes': prior['nodes']}
        value = service._mutation(request)
        if (value['state'] != 'cancelled' or not value['cancel_requested'] or not value['batch_id']
                or value['current_stage'] != ('build_finalizing' if scope=='build-native/v1' else 'reducing') or service.pending_pause(value)
                or value['dispatch_policy']['mode'] != 'full'):
            fail('INVALID_TRANSITION', 'requires a cancelled Reduce stage with its Pass pause already released')
        from native_stop_inventory import verify_cancel_drain
        verify_cancel_drain(adapter, wid, timeout=0)
        if any(s in ('ready', 'running', 'triage', 'todo') for s in adapter.kanban.task_states(value['kanban']['board_id']).values()):
            fail('WORKER_ACTIVE', 'native board must be nondispatchable')
        knowledge = service.knowledge
        with _exclusive_lock(knowledge._batch_lock_path(value['batch_id'])):
            batch = knowledge._batch(value['batch_id'])
            if (batch['state'] != 'cancelled' or not batch['cancel_requested']
                    or any(s['lease']['worker_id'] or s['state'] == 'leased' for s in knowledge._slices(value['batch_id']))):
                fail('WORKER_ACTIVE', 'cancelled batch must have no live leases')
            service._verify_execution_evidence(value)
            from ingest_kanban import desired_graph, domain_completed, executor_for
            nodes = {n.name: n for n in desired_graph(service, value)}
            templates = service.pinned_templates(value)
            skipped = service.skipped_pass_tasks(value)
            audited = []
            for entry in entries:
                name = entry.get('node')
                node = nodes.get(name)
                permitted_kinds=('build-finalize',) if scope=='build-native/v1' else ('checkpoint-1-validate',) if scope=='reduce-validation/v1' else ('resource-reduce','global-reduce')
                if not node or node.kind not in permitted_kinds:
                    fail('INVALID_TRANSITION', 'only interrupted resource/global Reduce can recover')
                if domain_completed(service, value, node) and not review:
                    fail('INVALID_TRANSITION', 'reuse completed Reduce through trusted reconciliation, never regenerate it')
                if review and (node.kind!='resource-reduce' or not domain_completed(service,value,node)):
                    fail('INVALID_TRANSITION','source result review requires a completed resource reduction')
                previous_reports=[_load_json(_vault_path(service.vault,r['report_ref']))
                    for r in value.get('execution_recovery_history',[]) if name in r['nodes']]
                from hermes_source_units.execution_plan import execution_recovery_key
                requested_key=execution_recovery_key(scope,{'node':name,'previous_task_id':entry.get('task_id')})
                if any(r['request'].get('recovery_scope')==scope and any(execution_recovery_key(scope,e)==requested_key for e in r['entries']) for r in previous_reports):
                    fail('RECOVERY_EXHAUSTED', 'one cancellation recovery per exact native attempt; diagnose further failure')
                card = next((c for c in value['kanban']['task_map'] if c['node'] == name), None)
                key = f'ingest:{wid}:{node.kind}:{node.input_fingerprint}'
                if not card and scope in ('source-hold/v1','source-hold/v2','source-hold/v3'):
                    # A completed supplement narrows task_map. Authenticate the
                    # unchanged canonical card below, never fabricate a new binding.
                    path=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(key)}.json')
                    stored=_load_json(path)
                    card={'node':name,'task_id':stored['task_id'],'idempotency_key':key}
                if not card or card['task_id'] != entry.get('task_id') or card['idempotency_key'] != key:
                    fail('STALE_INPUT', 'selected native identity changed')
                ref = f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(name)[:16]}.json'
                data = _vault_path(service.vault, ref).read_bytes() if not review else b''
                report = json.loads(data) if not review else {}
                if not review and (entry.get('failure_sha256') != hashlib.sha256(data).hexdigest()
                        or report.get('node') != name or report.get('task_id') != card['task_id']
                        or report.get('input_fingerprint') != node.input_fingerprint
                        or report.get('idempotency_key') != key):
                    fail('STALE_INPUT', 'failure evidence or binding changed')
                projection=scope=='reduce-cancel/v1' and report.get('error_code')=='DISPATCH_ARTIFACT_CHANGED'
                call_failure=scope in ('reduce-cancel/v1','build-native/v1') and report.get('error_code') in ('NATIVE_REQUEST_REJECTED','NATIVE_COMPLETION_UNKNOWN','PROGRAM_EXECUTION_FAILED')
                allowed=(('SEMANTIC_REVIEW_REQUIRED','SEMANTIC_OUTPUT_HELD','INVALID_SCHEMA','DUPLICATE','INCOMPLETE_COVERAGE','UNRESOLVED_REFERENCE','IDENTITY_CONFLICT','NATIVE_COMPLETION_UNKNOWN','NATIVE_WORKER_STOPPED','WORKFLOW_STOPPED') if scope in ('reduce-disposition/v1','reduce-coverage/v1') else ('INVALID_SCHEMA','DUPLICATE','INCOMPLETE_COVERAGE','UNRESOLVED_REFERENCE','IDENTITY_CONFLICT','INVALID_TRANSITION','READING_WINDOW_OVERSIZE') if scope=='semantic-output/v1' else ('DEPENDENCY_EXECUTION_BLOCKED',) if scope=='dependency-resolved/v1' else ('SEMANTIC_REVIEW_REQUIRED',) if scope=='source-format/v1' else ('SEMANTIC_REVIEW_REQUIRED','PROGRAM_EXECUTION_FAILED') if scope in ('source-hold/v1','source-hold/v2','source-hold/v3') else
                    ('READING_WINDOW_OVERSIZE',) if scope=='source-packets/v1' else ('NATIVE_WORKER_STOPPED','WORKFLOW_STOPPED','DISPATCH_ARTIFACT_CHANGED'))
                if scope=='reduce-coverage/v1':allowed=(*allowed,'REDUCTION_FAILED')
                local_amendment=scope=='reduce-cancel/v1' and bool(request.get('amendment_id'))
                if local_amendment:allowed=(*allowed,'RECOVERY_NOT_ALLOWED','CITATION_AMENDMENT_HELD')
                if scope=='reduce-validation/v1':allowed=('COVERAGE_SCOPE_INVALID','VALIDATION_FAILED','PROGRAM_EXECUTION_FAILED','NATIVE_WORKER_STOPPED','WORKFLOW_STOPPED')
                if scope=='build-native/v1':allowed=('NATIVE_WORKER_STOPPED','WORKFLOW_STOPPED','SEMANTIC_REVIEW_REQUIRED')
                if not review and ((report.get('error_code') not in allowed and not call_failure) or (scope in ('source-packets/v1','source-hold/v1','source-hold/v2','source-hold/v3') and node.kind!='resource-reduce')):
                    fail('RECOVERY_NOT_ALLOWED', 'semantic, material, budget and unknown failures remain held')
                snap = adapter.kanban.task_snapshot(value['kanban']['board_id'], card['task_id'])
                task, runs = snap.get('task', {}), snap.get('runs', [])
                if (task.get('id') != card['task_id'] or task.get('worker_pid') or (not runs and not projection and scope!='dependency-resolved/v1')
                        or any(not r.get('ended_at') for r in runs)):
                    fail('WORKER_ACTIVE', 'native attempt must be verifiably ended')
                path = _vault_path(service.vault, f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(key)}.json')
                binding = _load_json(path)
                original=cancelled_projection_binding(adapter,value,node,card,report,binding,path,snap) if projection else None
                body = json.loads(task.get('body', '{}'))
                template_hash = next(p['template_hash'] for p in value['template_pins'] if p['kind'] == node.kind)
                expected_executor='program-v1' if scope=='reduce-validation/v1' else 'semantic-v1'
                if (task.get('created_by') != 'ingest-workflow'
                        or executor_for(node.kind, templates[node.kind]) != expected_executor
                        or any(binding.get(k) != v for k, v in {
                            'workflow_id': wid, 'node': name, 'task_id': binding['task_id'] if projection else card['task_id'], 'actor': value['actor'],
                            'input_fingerprint': node.input_fingerprint, 'template_hash': template_hash}.items())
                        or any(body.get(k) != v for k, v in {
                            'workflow_id': wid, 'node': name, 'vault': str(service.vault), 'executor': expected_executor,
                            'input_fingerprint': node.input_fingerprint, 'worker_binding': str(path),
                            'template_hash': template_hash}.items())):
                    fail('STALE_INPUT', 'native card or pinned worker binding changed')
                # Validate every current task's original measurement/authority;
                # skipped ranges are excluded by the reducer, not made valid here.
                for tid in batch['task_ids']:
                    if tid in skipped:
                        continue
                    task_record = knowledge._task(tid)
                    if (node.kind == 'resource-reduce' and
                            {r['unit_ref']['resource_id'] for r in task_record['target_refs']} != {name.partition(':')[2]}):
                        continue
                    knowledge._verify_planned_measurement(task_record, batch['actor'],
                                                          batch['document_registry_revision'], None)
                audited.append({'node': name, 'previous_task_id': card['task_id'],
                    'previous_input_fingerprint': node.input_fingerprint, 'failure_ref': ref,
                    'failure_sha256': hashlib.sha256(data).hexdigest(), 'failure': report,
                    'ended_run_ids': [str(r['id']) for r in runs], 'template_hash': template_hash})
                if local_amendment:
                    from citation_amendment_compat import verify_execution_recovery
                    audited[-1].update(verify_execution_recovery(adapter,value,request,binding))
                if scope=='reduce-coverage/v1':
                    from reduce_coverage import verify
                    audited[-1].update(verify(adapter,value,node,binding,entry,report,runs))
                if scope=='reduce-validation/v1':
                    from reduce_coverage import verify_validator
                    audited[-1].update(verify_validator(adapter,value,node,binding,entry))
                if call_failure:
                    from native_call_state import recovery_evidence
                    audited[-1].update(recovery_evidence(adapter,value,binding,runs))
                if scope=='build-native/v1':
                    from build_execution_recovery import verify
                    audited[-1].update(verify(adapter,value,node,binding,entry,runs))
                if scope=='semantic-output/v1':
                    from semantic_output_recovery import verify
                    audited[-1].update(verify(adapter,value,node,binding,entry,report,runs))
                if scope=='reduce-disposition/v1':
                    from reduce_disposition import verify
                    audited[-1].update(verify(adapter,value,node,binding,entry,report,runs))
                if entry.get('stop_after_output') is not None:
                    if scope!='reduce-cancel/v1':fail('INVALID_SCHEMA','output stop limit belongs to existing cancellation recovery')
                    from semantic_output import verify_stop_boundary
                    audited[-1].update(verify_stop_boundary(adapter,wid,name,entry))
                if review:
                    from source_result_review import verify
                    audited[-1].update(verify(adapter,value,node,binding,entry))
                    for key in ('failure_ref','failure_sha256','failure'):audited[-1].pop(key)
                    audited[-1]['review_reason']=request['reason']
                if scope in ('source-hold/v1','source-hold/v2','source-hold/v3'):
                    from source_hold_recovery import verify
                    audited[-1].update(verify(adapter,value,node,binding,entry,report,runs))
                if scope in ('source-format/v1','dependency-resolved/v1'):
                    from reduce_target_recovery import verify
                    audited[-1].update(verify(adapter,value,node,binding,entry,report,runs,scope))
                if original:
                    audited[-1]['cancelled_projection']={'canonical_task_id':binding['task_id'],
                        'canonical_binding_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                        'cancelled_zero_execution_run_ids':[str(r['id']) for r in original['runs']],
                        'replacement_attempts':0,'semantic_retry_authorized':False}
                if scope=='source-packets/v1':
                    import fixed_reduce
                    from fixed_reduce_packets import plan, CONTRACT
                    from fixed_semantic import prepare_request_projection
                    preview=fixed_reduce.preview_snapshot(adapter,binding)
                    packets=plan(adapter,preview,prepare_request_projection)
                    audited[-1]['corrected_input']={'contract':CONTRACT,'snapshot_sha256':preview['input_id'],
                        'packets':[{k:v for k,v in p.items() if k!='view'} for p in packets]}
            result = {'ok': True, 'workflow_id': wid, 'recovery_id': rid, 'applied': not dry_run,
                      'nodes': [e['node'] for e in audited], 'state': 'cancelled', 'resumed': False}
            if dry_run:
                return {**result, 'revision': value['revision'], 'entries': audited}
            ref = f'_system/ledgers/ingest-workflows/{wid}/execution-recoveries/{rid}.json'
            report = {'request': dict(request), 'entries': audited, 'batch_id': value['batch_id'],
                      'domain_inputs_changed': False, 'semantic_retry_authorized': scope in ('source-hold/v1','source-hold/v2','source-hold/v3','source-format/v1','source-result-review/v1') or
                          scope=='reduce-disposition/v1' or
                          (scope=='semantic-output/v1' and any(not e.get('uncalled_packet') for e in audited))}
            path = _vault_path(service.vault, ref)
            data = _json_bytes(report)
            if path.exists() and path.read_bytes() != data:
                fail('IDEMPOTENCY_CONFLICT', 'recovery evidence differs')
            _write_atomic(path, data)
            value.setdefault('execution_recovery_history', []).append({
                'recovery_id': rid, 'input_digest': request['input_digest'], 'report_ref': ref,
                'report_sha256': hashlib.sha256(data).hexdigest(), 'nodes': result['nodes']})
            value['revision'] += 1
            service._write(value)
            return {**result, 'revision': value['revision'], 'report_ref': ref}
