"""Native adapters around supported operator and bound Pass operations."""
from collections import Counter
import hashlib
import os
from pathlib import Path
import re

from hermes_source_units import ContractError, FileIngestWorkflowService, mutation_digest
from hermes_source_units.model_presentation import present_model_packet
from hermes_source_units.semantic_submission import validate_native_submission
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_guard import WORKER_LOCK_TIMEOUT
from ingest_kanban import IngestKanbanAdapter
from native_pass_identity import (bind_payload, capture_context, load_context,
    remember_candidates, verified_packet, visible_packet, evidence_packet, pass_failure)


def fail(code, message):
    raise ContractError(code, '$', message)


def operator_only():
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('ACCESS_DENIED', 'workflow control requires a trusted operator outside workers')


def save_binding(path, binding):
    if path.is_symlink() or not path.parent.is_dir():
        fail('ACCESS_DENIED', 'binding must be a regular file in the native workspace')
    _write_atomic(path, _json_bytes(binding))
    os.chmod(path, 0o600)


def worker_context(vault, *, beginning=False):
    task_id = os.environ.get('HERMES_KANBAN_TASK')
    raw = os.environ.get('HERMES_KANBAN_WORKSPACE')
    if not task_id or not raw:
        fail('ACCESS_DENIED', 'native Pass tools require a native task workspace')
    workspace = Path(raw).resolve(strict=True)
    path = workspace / 'worker-request.json'
    adapter = IngestKanbanAdapter(vault, enable_workers=True)
    if path.is_file():
        if path.is_symlink():
            fail('ACCESS_DENIED', 'worker binding cannot be a symlink')
        binding = _load_json(path)
    elif beginning:
        matches = []
        for file in (adapter.workflow.vault/'_system/ledgers/ingest-workflows').glob('*.json'):
            workflow = _load_json(file)
            for card in workflow.get('kanban', {}).get('task_map', []):
                if card.get('task_id') == task_id:
                    matches.append((workflow, card))
        if len(matches) != 1:
            fail('STALE_INPUT', 'native card has no unique workflow binding in this Vault')
        workflow, card = matches[0]
        ref = (f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}/bindings/"
               f"{fingerprint(card['idempotency_key'])}.json")
        binding = _load_json(_vault_path(adapter.workflow.vault, ref))
    else:
        fail('STALE_INPUT', 'missing checked worker-request.json; begin this Pass first')
    if binding.get('task_id') != task_id or not binding.get('node', '').startswith('pass-slice:'):
        fail('ACCESS_DENIED', 'binding does not belong to this native Pass card')
    adapter.validate_worker_request(binding, beginning=beginning)
    return adapter, workspace, path, binding


def read_pass_input(vault, *, begin=False):
    adapter, workspace, binding_path, binding = worker_context(vault, beginning=begin)
    descriptor_path = workspace/'pass-input-descriptor.json'
    fresh = False
    if begin and not binding_path.exists():
        result = adapter.worker_begin(binding)
        if not result.get('leased'):
            reason = result.get('reason', 'not_leased')
            stopped = reason == 'batch_cancelled'
            waiting = reason in ('batch_cooldown','concurrency_limit','slice_leased')
            code = 'PASS_ADMISSION_WAIT' if waiting else 'PASS_ADMISSION_RECOVERY'
            receipt = {'ok':True,'state':'stopped' if stopped else 'waiting' if waiting else 'recovery_required',
                'reason':reason,'code':code,'next_action':'end_worker',
                'recovery_owner':'trusted_operator','retry_same_request':False}
            if not stopped:
                # Park this exact native binding; never take another worker's lease.
                receipt['report_ref'] = adapter.execution_failure(binding, code, reason)
            return receipt
        if not result.get('model_input'):
            bound = result.get('worker_request')
            if bound:
                outcome = adapter.worker_fail({**bound,'code':'NATIVE_INPUT_UNAVAILABLE',
                    'message':'leased worker has no bounded model input; supported recovery required'})
                receipt = pass_failure(ContractError('NATIVE_INPUT_UNAVAILABLE','$',
                    'leased worker has no bounded model input'))
                receipt.update(lease_released=outcome['ok'],
                    kanban_reconciliation_pending=outcome['kanban_reconciliation_pending'])
                return receipt
            fail('NATIVE_INPUT_UNAVAILABLE', 'leased worker has no bounded model input')
        save_binding(binding_path, result['worker_request'])
        binding = result['worker_request']
        _write_atomic(descriptor_path, _json_bytes(result['model_input']))
        fresh = True
    checked = adapter.worker_check(binding)
    if descriptor_path.is_symlink() or not descriptor_path.is_file():
        fail('STALE_INPUT', 'missing original bounded packet descriptor; do not reconstruct it')
    descriptor = _load_json(descriptor_path)
    packet_path, packet = verified_packet(adapter.workflow.vault, descriptor, checked)
    service = adapter.workflow.knowledge
    if not fresh:
        # Replay never trusts a model-editable descriptor as reading authority.
        service.source.enable_session_cache()
        prepared = service.prepare_leased_slice(checked['batch_id'], checked['slice_id'],
            binding['worker_id'], binding['expected_revision'], check=lambda: adapter.worker_check(binding),
            timeout=WORKER_LOCK_TIMEOUT)
        projections = [service.model_input(p['task_id'], _load_json(_vault_path(service.vault, p['path'])))
                       for p in prepared['reading_packages']]
        live = service.model_packet(projections, checked['batch_id'])
        # Semantic progress may change continuation, never reading authority.
        if evidence_packet(live['view']) != evidence_packet(packet):
            fail('STALE_INPUT', 'bounded presentation changed; do not replace its evidence silently')
    context = capture_context(adapter.workflow.vault, binding, packet_path, packet)
    presented = packet if fresh else live['view']
    if not fresh:
        remember_candidates(context, {'results':[{'task':t['task'],'sequence':0,
            'pass_id':t['continuation']['candidate']['candidate_pass_id']}
            for t in presented['tasks'] if t['continuation']['action']=='citation']})
    text = present_model_packet(visible_packet(presented))
    limit = service._batch(checked['batch_id'])['slice_config']['slice_max_input_codepoints']
    import json
    receipt = {'ok':True,'renderer':packet['contract'],'task_count':len(packet['tasks']),
               'input_codepoints':0,'packet_sha256':hashlib.sha256(packet_path.read_bytes()).hexdigest(),
               'presentation_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest()}
    while True:
        size = len(text)+len(json.dumps(receipt,ensure_ascii=False))+1
        if receipt['input_codepoints']==size:
            break
        receipt['input_codepoints']=size
    if size > limit:
        fail('READING_WINDOW_OVERSIZE', 'readable presentation exceeds the bound slice budget')
    return {**receipt,'content':text}


def compact_semantic_receipt(vault, binding, result, task_ids):
    ref = (f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/worker-receipts/"
           f"{fingerprint(result)}.json")
    _write_atomic(_vault_path(Path(vault).resolve(), ref), _json_bytes(result))
    keep = ('task','created','sequence','pass_kind','next_sequence','next_action')
    aliases = {tid:f't{i}' for i,tid in enumerate(task_ids,1)}
    receipt = {'ok':result['ok'], 'validated':result.get('validated', result['ok']),
            'results':[{k:r[k] for k in keep if k in r} for r in result['results']],
            'failures':[{**{k:v for k,v in f.items() if k != 'item_id'},
                'task':aliases.get(f.get('item_id'))} for f in result['failures']], 'receipt_ref':ref}
    if not result['ok']:
        decisions = [pass_failure(ContractError(f['code'],'$',f['message']),semantic=True) for f in result['failures']]
        decision = next((d for d in decisions if d['state']!='draft_rejected'),decisions[0])
        receipt.update({k:decision[k] for k in ('state','next_action','recovery_owner','retry_same_request')})
    return receipt


def _worker_action(vault, action, payload=None):
    if action in ('begin','read'):
        return read_pass_input(vault, begin=action=='begin')
    if action in ('submit','confirm'):
        try:
            validate_native_submission(payload, confirmation=action=='confirm')
        except ContractError as exc:
            return pass_failure(exc,semantic=True)
    adapter, workspace, binding_path, binding = worker_context(vault)
    if action == 'complete':
        result = adapter.worker_complete(binding)
        ref = (f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/worker-receipts/"
               f"complete-{fingerprint(result)}.json")
        _write_atomic(_vault_path(adapter.workflow.vault, ref), _json_bytes(result))
        return {'ok':result['ok'], 'domain_complete':True, 'result_ref_count':len(result['result_refs']),
                'receipt_ref':ref, 'kanban_reconciliation_pending':result['kanban_reconciliation_pending'],
                'next_action':'end_worker'}
    if action in ('submit','confirm'):
        checked = adapter.worker_check(binding)
        context_path, context = load_context(adapter.workflow.vault, binding, checked)
        try:
            payload = bind_payload(payload, context, confirmation=action=='confirm')
        except ContractError as exc:
            if exc.code == 'INVALID_SCHEMA':
                return pass_failure(exc,semantic=True)
            raise
    refreshed = adapter.worker_heartbeat(binding)
    binding = refreshed['worker_request']
    save_binding(binding_path, binding)
    if action == 'heartbeat':
        return {'ok':True, 'lease_revision':binding['expected_revision']}
    from bound_pass_submission import submit_bound
    result = submit_bound(vault, binding, payload, confirmation=action=='confirm', adapter=adapter)
    remember_candidates(context_path, result)
    receipt = compact_semantic_receipt(vault, binding, result, checked['task_ids'])
    if not receipt['ok'] and receipt['state'] != 'draft_rejected':
        try:
            failure = next(f for f in result['failures'] if pass_failure(
                ContractError(f['code'],'$',f['message']),semantic=True)['state'] != 'draft_rejected')
            outcome = adapter.worker_fail({**binding,'code':failure['code'],'message':failure['message']})
            receipt['lease_released'] = outcome['ok']
        except (ContractError,OSError,ValueError,TypeError,KeyError):
            receipt['lease_released'] = False
    return receipt


def worker_action(vault, action, payload=None):
    try:
        return _worker_action(vault, action, payload)
    except (ContractError,OSError,ValueError,TypeError,KeyError) as exc:
        receipt = pass_failure(exc)
        if receipt['state'] not in ('draft_rejected','stopped'):
            try:
                adapter, workspace, path, binding = worker_context(vault)
                # Only a checked current lease can be failed. Stale/stopped
                # bindings cannot be repaired, refreshed or rewritten here.
                outcome = adapter.worker_fail({**binding,'code':receipt['code'],'message':str(exc)})
                receipt['lease_released'] = outcome['ok']
                receipt['kanban_reconciliation_pending'] = outcome['kanban_reconciliation_pending']
            except (ContractError,OSError,ValueError,TypeError,KeyError):
                receipt['lease_released'] = False
        return receipt


def workflow_status(vault, workflow_id=None, *, runtime=False):
    operator_only()
    service = FileIngestWorkflowService(vault)
    if workflow_id is None:
        values = [_load_json(p) for p in (service.vault/'_system/ledgers/ingest-workflows').glob('*.json')]
        values = [v for v in values if v.get('state') not in ('completed','partial','failed')]
        if len(values) != 1:
            return {'ok':False,'code':'AMBIGUOUS_WORKFLOW',
                    'error':'Select an observed workflow_id using the user-authorized scope and existing handoff.',
                    'candidates':[{'workflow_id':v['workflow_id'],'state':v['state'],
                        'stage':v['current_stage'],'batch_id':v.get('batch_id'),
                        'execution_mode':v['scope'].get('execution_mode'),
                        'source_paths':v['scope'].get('source_paths',[])} for v in values]}
        workflow_id = values[0]['workflow_id']
    value = service.status(workflow_id)
    coverage = service.source_coverage(value)
    revision = value.get('pass_revision')
    plan = service.execution_plan(value)
    out = {'ok':True, 'workflow_id':value['workflow_id'], 'revision':value['revision'], 'state':value['state'],
           'stage':value['current_stage'], 'batch_id':value['batch_id'], 'pause':service.pause_status(value),
           'current_pass_revision':({k:revision[k] for k in ('revision_id','state')} if revision else None),
           'execution_config':plan['config'] if plan else None,
           'execution_journal_pending':service._execution_journal(workflow_id).exists(),
           'source_counts':{key:len(coverage[key]) for key in ('ready','failed','pending')}}
    if value['batch_id']:
        batch = service.knowledge._batch(value['batch_id'])
        slices = service.knowledge._slices(value['batch_id'])
        out.update(batch_revision=batch['revision'], batch_state=batch['state'],
                   slice_counts=dict(Counter(s['state'] for s in slices)), total_slices=len(slices),
                   active_leases=sum(bool(s['lease']['worker_id']) for s in slices),
                   pass_records=sum(len(list((service.vault/f'_system/knowledge-builds/task-{tid}/passes').glob('*.json')))
                                    for tid in batch['task_ids']),
                   reduce_run_count=len(batch.get('run_ids', [])), release_id=value.get('release_id'))
    if runtime and value['kanban']['board_id']:
        states = IngestKanbanAdapter(vault).kanban.task_states(value['kanban']['board_id'])
        out['native_counts'] = dict(Counter(states.get(c['task_id'],'not_in_active_listing')
                                           for c in value['kanban']['task_map']))
    return out


def control_workflow(vault, workflow_id, action, operation_id):
    operator_only()
    if action not in ('resume','cancel') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', operation_id):
        fail('INVALID_SCHEMA', 'control needs resume/cancel and a stable safe operation_id')
    service = FileIngestWorkflowService(vault)
    value = service.status(workflow_id)
    root = _vault_path(service.vault,
        f'_system/ledgers/ingest-workflows/{workflow_id}/operator-operations/{operation_id}')
    intent = {'workflow_id':workflow_id,'action':action,'operation_id':operation_id}
    with _exclusive_lock(root/'.operation.lock'):
        request_path, result_path = root/'request.json', root/'result.json'
        if request_path.exists():
            saved = _load_json(request_path)
            if saved['intent'] != intent:
                fail('IDEMPOTENCY_CONFLICT', 'operation_id already names another authorized intent')
            if not result_path.exists():
                fail('EXECUTION_UNCERTAIN', 'saved operation has no receipt; observe before recovery')
            return {**_load_json(result_path),'replayed':True}
        request = {'workflow_id':workflow_id,'actor':value['actor'],'expected_revision':value['revision']}
        request['input_digest'] = mutation_digest(request)
        _write_atomic(request_path, _json_bytes({'intent':intent,'request':request}))
        from orchestration import dispatch
        result = dispatch(vault, action, request)
        receipt = {'ok':True,'action':action,'operation_id':operation_id,
                   'request_ref':request_path.relative_to(service.vault).as_posix(),
                   'dispatch':result,'status':workflow_status(vault, workflow_id)}
        _write_atomic(result_path, _json_bytes(receipt))
        return receipt
