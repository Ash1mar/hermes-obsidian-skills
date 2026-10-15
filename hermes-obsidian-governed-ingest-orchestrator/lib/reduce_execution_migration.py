"""Explicit fixed-executor migration before a legacy workflow's first Reduce."""
import hashlib
import json
import os
import re

from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import fingerprint, record_validation_scope

KINDS = ('resource-reduce', 'global-reduce', 'checkpoint-1-validate')


def fail(code, message):
    raise ContractError(code, '$', message)


def target_templates():
    from orchestration import worker_pack
    return {p['kind']:p for p in worker_pack() if p['kind'] in KINDS}


def target_hashes():
    return {k:'sha256:'+hashlib.sha256(p['content'].encode('utf-8')).hexdigest()
            for k,p in target_templates().items()}


def migrate_reduce_execution(adapter, request, *, dry_run=False):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('ACCESS_DENIED', 'Reduce migration requires a trusted operator')
    service = adapter.workflow
    service._check_request(request)
    wid, mid = request['workflow_id'], request.get('migration_id', '')
    if not isinstance(mid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', mid):
        fail('INVALID_SCHEMA', 'invalid migration ID')
    if request.get('migration_scope') != 'reduce-fixed/v1':
        fail('INVALID_SCHEMA', 'migration requires reduce-fixed/v1')
    if not isinstance(request.get('reason'), str) or not request['reason'].strip():
        fail('INVALID_SCHEMA', 'describe the authorized migration')
    lock = _vault_path(service.vault, f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock')
    with _exclusive_lock(lock), _exclusive_lock(service._lock(wid)), record_validation_scope():
        value = service.status(wid)
        if request['actor'] != value['actor']:
            fail('ACTOR_MISMATCH', 'migration actor differs')
        service.assert_execution_ready(value)
        history = value.get('reduce_migration_history', [])
        if history:
            previous = history[0]
            if previous['migration_id'] == mid and previous['input_digest'] == request['input_digest']:
                return {'ok':True, 'replayed':True, 'applied':False, 'resumed':False,
                        'workflow_id':wid, 'revision':value['revision'], 'report_ref':previous['report_ref']}
            fail('IDEMPOTENCY_CONFLICT', 'Reduce execution was already migrated')
        value = service._mutation(request)
        if (value['state'] != 'cancelled' or not value['cancel_requested'] or not value['batch_id']
                or value['current_stage'] != 'reducing' or service.pending_pause(value)
                or value['dispatch_policy']['mode'] != 'full'
                or ('pass' in value['scope'].get('pause_after', [])
                    and 'pass' not in service.pause_status(value)['continued'])):
            fail('INVALID_TRANSITION', 'requires cancelled, released Reduce before its first attempt')
        from native_stop_inventory import verify_cancel_drain
        verify_cancel_drain(adapter, wid, timeout=0)
        if any(s in ('ready','running','triage','todo')
               for s in adapter.kanban.task_states(value['kanban']['board_id']).values()):
            fail('WORKER_ACTIVE', 'native board must be nondispatchable')
        knowledge = service.knowledge
        with _exclusive_lock(knowledge._batch_lock_path(value['batch_id'])):
            batch = knowledge._batch(value['batch_id'])
            if (batch['state'] != 'cancelled' or not batch['cancel_requested']
                    or any(s['lease']['worker_id'] or s['state']=='leased'
                           for s in knowledge._slices(value['batch_id']))):
                fail('WORKER_ACTIVE', 'cancelled batch must have no live leases')
            if (batch['resource_reduction_ids'] or batch.get('global_reduction_id') or batch['run_ids']
                    or value.get('release_id') or value['checkpoints']['checkpoint_1']['state'] != 'pending'):
                fail('MIGRATION_NOT_ALLOWED', 'existing Reduce or checkpoint results must be preserved without migration')
            service._verify_execution_evidence(value)
            skipped = service.skipped_pass_tasks(value)
            for tid in batch['task_ids']:
                if tid in skipped:
                    continue
                task = knowledge._task(tid)
                knowledge._verify_planned_measurement(task, batch['actor'], batch['document_registry_revision'], None)
                if not service._task_passes_complete(tid):
                    fail('INCOMPLETE_COVERAGE', 'all non-skipped Pass work must be complete')
            from ingest_kanban import desired_graph, executor_for, domain_completed
            graph = {n.name:n for n in desired_graph(service, value)}
            old_pins = [dict(p) for p in value['template_pins'] if p['kind'] in KINDS]
            if request.get('previous_pins_digest') != 'sha256:'+fingerprint(old_pins):
                fail('STALE_INPUT', 'selected active Reduce pins changed')
            templates = target_templates()
            hashes = target_hashes()
            if request.get('target_template_hashes') != hashes:
                fail('STALE_INPUT', 'installed target templates differ from the reviewed request')
            old_content = service.pinned_templates(value)
            if (all(p['template_hash']==hashes[p['kind']] for p in old_pins)
                    or not any(executor_for(k, old_content[k])=='model' for k in KINDS)):
                fail('MIGRATION_NOT_ALLOWED', 'migration requires legacy Reduce contracts')
            audited = []
            for card in value['kanban']['task_map']:
                node = graph.get(card['node'])
                if not node or node.kind not in KINDS:
                    continue
                key = f'ingest:{wid}:{node.kind}:{node.input_fingerprint}'
                if card['idempotency_key'] != key or domain_completed(service,value,node):
                    fail('STALE_INPUT', 'selected Reduce binding or outcome changed')
                failure = _vault_path(service.vault,
                    f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(node.name)[:16]}.json')
                if failure.is_file() and _load_json(failure).get('task_id') == card['task_id']:
                    fail('MIGRATION_NOT_ALLOWED', 'failed work requires diagnosis, not contract migration')
                snap = adapter.kanban.task_snapshot(value['kanban']['board_id'], card['task_id'])
                native, runs = snap['task'], snap['runs']
                # Cancellation parking rows are administrative, not worker attempts.
                if (native.get('worker_pid') or any(not (
                        run.get('status')=='scheduled' and run.get('outcome')=='scheduled'
                        and run.get('summary')=='Vault workflow cancelled' and not run.get('worker_pid')
                        and run.get('ended_at') and run.get('started_at')==run['ended_at']) for run in runs)):
                    fail('MIGRATION_NOT_ALLOWED', 'native Reduce must not have started')
                path = _vault_path(service.vault,
                    f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(key)}.json')
                binding = _load_json(path)
                body = json.loads(native.get('body','{}'))
                old_hash = next(p['template_hash'] for p in old_pins if p['kind']==node.kind)
                expected = {'workflow_id':wid,'node':node.name,'task_id':card['task_id'],
                            'actor':value['actor'],'input_fingerprint':node.input_fingerprint,'template_hash':old_hash}
                if (native.get('id')!=card['task_id'] or native.get('created_by')!='ingest-workflow'
                        or any(binding.get(k)!=v for k,v in expected.items())
                        or any(body.get(k)!=v for k,v in {
                            'workflow_id':wid,'node':node.name,'vault':str(service.vault),
                            'input_fingerprint':node.input_fingerprint,'template_hash':old_hash,
                            'worker_binding':str(path),'executor':executor_for(node.kind,old_content[node.kind])}.items())):
                    fail('STALE_INPUT', 'native card or immutable binding changed')
                audited.append({'node':node.name,'previous_task_id':card['task_id'],
                    'previous_input_fingerprint':node.input_fingerprint,
                    'binding_ref':path.relative_to(service.vault).as_posix(),
                    'binding_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'administrative_runs':runs})
            if {n.name for n in graph.values() if n.kind in KINDS} != {e['node'] for e in audited}:
                fail('STALE_INPUT', 'current Reduce graph lacks complete native bindings')
            new_pins, payloads = [], []
            for kind in KINDS:
                template = templates[kind]
                expected_executor = 'program-v1' if kind=='checkpoint-1-validate' else 'semantic-v1'
                if executor_for(kind,template['content']) != expected_executor:
                    fail('INVALID_SCHEMA', 'target does not authorize the fixed executor')
                ref = f'_system/ledgers/ingest-workflows/{wid}/templates/{kind}-{hashes[kind][7:]}.md'
                new_pins.append({'kind':kind,'template_id':template['template_id'],
                                 'template_hash':hashes[kind],'path':ref})
                payloads.append((ref,template['content'].encode('utf-8')))
            result = {'ok':True,'applied':not dry_run,'workflow_id':wid,'migration_id':mid,
                      'state':'cancelled','resumed':False,'old_pins':old_pins,'new_pins':new_pins,
                      'nodes':[e['node'] for e in audited]}
            if dry_run:
                return {**result,'revision':value['revision']}
            report_ref = f'_system/ledgers/ingest-workflows/{wid}/reduce-migrations/{mid}.json'
            report = {'request':dict(request),'old_pins':old_pins,'new_pins':new_pins,'entries':audited,
                      'batch_id':batch['batch_id'],'domain_inputs_changed':False,'resumed':False}
            payloads.append((report_ref,_json_bytes(report)))
            for ref,data in payloads:
                path = _vault_path(service.vault,ref)
                if path.exists() and path.read_bytes()!=data:
                    fail('IDEMPOTENCY_CONFLICT', 'immutable migration payload differs')
            for ref,data in payloads:
                path = _vault_path(service.vault,ref)
                if not path.exists():
                    _write_atomic(path,data)
            replacements = {p['kind']:p for p in new_pins}
            value['template_pins'] = [replacements.get(p['kind'],p) for p in value['template_pins']]
            value['reduce_migration_history'] = [{'migration_id':mid,'input_digest':request['input_digest'],
                'report_ref':report_ref,'report_sha256':hashlib.sha256(_json_bytes(report)).hexdigest()}]
            value['revision'] += 1
            service._write(value)
            return {**result,'revision':value['revision'],'report_ref':report_ref}
