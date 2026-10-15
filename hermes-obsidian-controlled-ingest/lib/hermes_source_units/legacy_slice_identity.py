"""Authenticate original slice configuration without rewriting historical identity."""
import hashlib
import json

from .source_units import _json_bytes, _load_json, _vault_path
from .validation import ContractError, fingerprint, validate_record

SCOPE = 'legacy-slice-config/v1'


def fail(message):
    raise ContractError('STALE_INPUT', '$', message)


def slice_fingerprint(batch_id, item, inputs, config):
    return 'sha256:' + fingerprint({'batch_id':batch_id, 'task_inputs':inputs,
        'slice_config':config, 'template_id':item['template_id'], 'template_hash':item['template_hash']})


def verify_snapshot(service, workflow, item, inputs, reference):
    """Exact-plan's released digest authenticates both batch and original slice bytes."""
    if not isinstance(reference,str) or not reference.strip():
        fail('historical snapshot reference is required')
    path = _vault_path(service.vault, reference)
    raw = path.read_bytes(); snapshot = _load_json(path)
    original_batch = snapshot.get('batch', {})
    original = next((s for s in snapshot.get('slices', []) if s.get('slice_id')==item['slice_id']), None)
    if not original or original_batch.get('batch_id')!=workflow['batch_id'] or original_batch.get('actor')!=workflow['actor']:
        fail('historical batch/slice identity differs')
    validate_record('knowledge_batch', original_batch)
    validate_record('knowledge_slice', original)
    control = snapshot.get('workflow', {}).get('pause_control', {})
    released = next((h for h in workflow.get('pause_control', {}).get('history', [])
                     if h['boundary']=='exact_plan'), None)
    if not released or control.get('boundary')!='exact_plan' or control.get('evidence_digest')!=released['evidence_digest']:
        fail('historical snapshot is not bound to the released exact plan')
    report = _load_json(_vault_path(service.vault, control.get('report_ref', '')))
    evidence = report.get('evidence', {})
    if (evidence.get('workflow_id')!=workflow['workflow_id'] or evidence.get('boundary')!='exact_plan'
            or report.get('evidence_digest')!=released['evidence_digest']
            or 'sha256:'+fingerprint(evidence)!=released['evidence_digest']):
        fail('historical exact-plan evidence changed')
    batch_ref = service.knowledge._batch_path(workflow['batch_id']).relative_to(service.vault).as_posix()
    slice_ref = service.knowledge._slice_path(workflow['batch_id'],item['slice_id']).relative_to(service.vault).as_posix()
    for ref, value in ((batch_ref,original_batch),(slice_ref,original)):
        if hashlib.sha256(_json_bytes(value)).hexdigest()!=evidence.get('files', {}).get(ref):
            fail('historical batch/slice bytes are not the exact-plan originals')
    for key in ('contract','batch_id','slice_id','task_ids','input_fingerprint','template_id','template_hash','input_codepoints','reslice_count'):
        if item.get(key)!=original.get(key):
            fail('historical slice ownership, template or creation identity changed')
    if item.get('reslice_count') or item['template_id']!='knowledge-pass/v1':
        fail('historical configuration recovery requires an original unresliced slice')
    config = original_batch['slice_config']
    if slice_fingerprint(workflow['batch_id'],item,inputs,config)!=item['input_fingerprint']:
        fail('current task inputs do not reproduce the authenticated historical slice')
    return config, {'snapshot_ref':reference,'snapshot_sha256':hashlib.sha256(raw).hexdigest(),
        'pause_report_ref':control['report_ref'],'evidence_digest':released['evidence_digest'],
        'slice_sha256':evidence['files'][slice_ref],'creation_config':config}


def creation_config(service, workflow, item, inputs, current):
    if slice_fingerprint(workflow['batch_id'],item,inputs,current)==item['input_fingerprint']:
        return current
    node='pass-slice:'+item['slice_id']
    for recovery in reversed(workflow.get('execution_recovery_history', [])):
        if node not in recovery['nodes']:continue
        data=_vault_path(service.vault,recovery['report_ref']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=recovery['report_sha256']:
            fail('historical configuration recovery evidence changed')
        report=json.loads(data)
        if report['request'].get('recovery_scope')!=SCOPE:continue
        config, proof=verify_snapshot(service,workflow,item,inputs,report['request']['historical_snapshot_ref'])
        entry=next(e for e in report['entries'] if e['node']==node)
        if proof!=entry.get('legacy_identity'):
            fail('historical configuration proof changed')
        return config
    fail('slice input fingerprint changed')
