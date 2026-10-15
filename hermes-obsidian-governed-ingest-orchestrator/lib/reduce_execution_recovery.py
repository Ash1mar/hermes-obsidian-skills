"""One explicit native retry after a cancelled Reduce attempt; domain inputs stay intact."""
import hashlib
import json
import os
import re

from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import fingerprint, record_validation_scope


def fail(code, message):
    raise ContractError(code, '$', message)


def recover_reduce_execution(adapter, request, *, dry_run=False):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('ACCESS_DENIED', 'Reduce recovery requires a trusted operator')
    service = adapter.workflow
    service._check_request(request)
    wid, rid = request['workflow_id'], request.get('recovery_id', '')
    if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', rid):
        fail('INVALID_SCHEMA', 'invalid recovery ID')
    scope=request.get('recovery_scope')
    if scope not in ('reduce-cancel/v1','source-packets/v1'):
        fail('INVALID_SCHEMA', 'Reduce recovery requires a supported explicit correction scope')
    if scope=='source-packets/v1':
        from fixed_reduce_packets import contract_hash
        if request.get('target_contract_sha256')!=contract_hash():
            fail('STALE_INPUT','source packet implementation differs from the reviewed request')
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
                or value['current_stage'] != 'reducing' or service.pending_pause(value)
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
                if not node or node.kind not in ('resource-reduce', 'global-reduce'):
                    fail('INVALID_TRANSITION', 'only interrupted resource/global Reduce can recover')
                if domain_completed(service, value, node):
                    fail('INVALID_TRANSITION', 'reuse completed Reduce through trusted reconciliation, never regenerate it')
                if any(name in r['nodes'] and _load_json(_vault_path(service.vault,r['report_ref']))['request'].get('recovery_scope')==scope
                       for r in value.get('execution_recovery_history', [])):
                    fail('RECOVERY_EXHAUSTED', 'one cancellation recovery per reducer; diagnose further failure')
                card = next((c for c in value['kanban']['task_map'] if c['node'] == name), None)
                key = f'ingest:{wid}:{node.kind}:{node.input_fingerprint}'
                if not card or card['task_id'] != entry.get('task_id') or card['idempotency_key'] != key:
                    fail('STALE_INPUT', 'selected native identity changed')
                ref = f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(name)[:16]}.json'
                data = _vault_path(service.vault, ref).read_bytes()
                report = json.loads(data)
                if (entry.get('failure_sha256') != hashlib.sha256(data).hexdigest()
                        or report.get('node') != name or report.get('task_id') != card['task_id']
                        or report.get('input_fingerprint') != node.input_fingerprint
                        or report.get('idempotency_key') != key):
                    fail('STALE_INPUT', 'failure evidence or binding changed')
                allowed=('READING_WINDOW_OVERSIZE',) if scope=='source-packets/v1' else ('NATIVE_WORKER_STOPPED','WORKFLOW_STOPPED')
                if report.get('error_code') not in allowed or (scope=='source-packets/v1' and node.kind!='resource-reduce'):
                    fail('RECOVERY_NOT_ALLOWED', 'semantic, material, budget and unknown failures remain held')
                snap = adapter.kanban.task_snapshot(value['kanban']['board_id'], card['task_id'])
                task, runs = snap.get('task', {}), snap.get('runs', [])
                if (task.get('id') != card['task_id'] or task.get('worker_pid') or not runs
                        or any(not r.get('ended_at') for r in runs)):
                    fail('WORKER_ACTIVE', 'native attempt must be verifiably ended')
                path = _vault_path(service.vault, f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(key)}.json')
                binding = _load_json(path)
                body = json.loads(task.get('body', '{}'))
                template_hash = next(p['template_hash'] for p in value['template_pins'] if p['kind'] == node.kind)
                if (task.get('created_by') != 'ingest-workflow'
                        or executor_for(node.kind, templates[node.kind]) != 'semantic-v1'
                        or any(binding.get(k) != v for k, v in {
                            'workflow_id': wid, 'node': name, 'task_id': card['task_id'], 'actor': value['actor'],
                            'input_fingerprint': node.input_fingerprint, 'template_hash': template_hash}.items())
                        or any(body.get(k) != v for k, v in {
                            'workflow_id': wid, 'node': name, 'vault': str(service.vault), 'executor': 'semantic-v1',
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
                      'domain_inputs_changed': False, 'semantic_retry_authorized': False}
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
