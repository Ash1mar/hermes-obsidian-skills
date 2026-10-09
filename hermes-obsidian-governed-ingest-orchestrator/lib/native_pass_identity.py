"""Native input snapshots own mechanical identities, never semantic decisions."""
import copy
import hashlib
from pathlib import Path

from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock, _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import fingerprint


def fail(code, message):
    raise ContractError(code, '$', message)


def context_path(vault, binding):
    owner = {k:binding[k] for k in ('workflow_id','node','task_id','worker_id','actor','template_hash')}
    ref = f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/native-inputs/{fingerprint(owner)}.json"
    return _vault_path(vault, ref), owner


def verified_packet(vault, descriptor, checked):
    """Only the immutable canonical packet/manifest for this exact ordered slice."""
    path = _vault_path(vault, descriptor['path'])
    root = Path(vault)/'_system/knowledge-builds/model-packets'/checked['batch_id']
    if not path.is_relative_to(root) or path.is_symlink():
        fail('STALE_INPUT', 'descriptor is not a canonical packet in this batch')
    packet = _load_json(path)
    manifest = _load_json(path.with_suffix('.manifest.json'))
    bindings = manifest['bindings']
    expected = {f't{i}':tid for i,tid in enumerate(checked['task_ids'],1)}
    if (packet.get('contract') != 'bounded-model-packet/v1'
            or manifest.get('batch_id') != checked['batch_id']
            or manifest.get('view_hash') != fingerprint(packet)
            or manifest.get('packet_id') != fingerprint({'view':packet,'bindings':bindings})
            or path.stem != manifest['packet_id']
            or {alias:b['task_id'] for alias,b in bindings.items()} != expected
            or [t['task'] for t in packet['tasks']] != list(expected)
            or any(t['input_id'] != bindings[t['task']]['input_id'] for t in packet['tasks'])):
        fail('STALE_INPUT', 'bounded packet or native task mapping changed')
    return path, packet


def capture_context(vault, binding, packet_path, packet):
    path, owner = context_path(vault, binding)
    record = {'contract':'native-pass-input/v1','owner':owner,
        'packet_ref':packet_path.relative_to(vault).as_posix(),
        'packet_sha256':hashlib.sha256(packet_path.read_bytes()).hexdigest(),
        'input_ids':{t['task']:t['input_id'] for t in packet['tasks']},
        'candidates':{t['task']:t['continuation']['candidate']['candidate_pass_id']
            for t in packet['tasks'] if t['continuation']['action']=='citation'}}
    with _exclusive_lock(path.with_suffix('.lock')):
        if path.exists():
            existing = _load_json(path)
            if any(existing[k] != v for k,v in record.items() if k != 'candidates'):
                fail('STALE_INPUT', 'original native input changed; supported recovery required')
            if any(existing['candidates'].get(k) != v for k,v in record['candidates'].items()):
                fail('STALE_INPUT', 'original reviewed candidate changed')
        else:
            _write_atomic(path, _json_bytes(record))
    return path


def load_context(vault, binding, checked):
    path, owner = context_path(vault, binding)
    if not path.is_file() or path.is_symlink():
        fail('NATIVE_INPUT_REQUIRED', 'read verified native input before semantic submission')
    record = _load_json(path)
    if record.get('contract') != 'native-pass-input/v1' or record['owner'] != owner:
        fail('STALE_INPUT', 'native input owner changed')
    packet_path, packet = verified_packet(vault, {'path':record['packet_ref']}, checked)
    if (hashlib.sha256(packet_path.read_bytes()).hexdigest() != record['packet_sha256']
            or record['input_ids'] != {t['task']:t['input_id'] for t in packet['tasks']}):
        fail('STALE_INPUT', 'original native input snapshot changed')
    return path, record


def bind_payload(payload, record, *, confirmation=False):
    key = 'confirmations' if confirmation else 'passes'
    result = copy.deepcopy(payload)
    seen = set()
    for item in result[key]:
        alias = item['task']
        if alias not in record['input_ids'] or alias in seen:
            fail('INVALID_SCHEMA', 'task must be unique within this bound input')
        seen.add(alias)
        expected = {'input_id':record['input_ids'][alias]}
        if confirmation:
            candidate = record['candidates'].get(alias)
            if candidate is None:
                fail('CANDIDATE_NOT_OBSERVED', 'review the original candidate from native input or your submission receipt first')
            expected['candidate_pass_id'] = candidate
        for field, value in expected.items():
            if field in item and item[field] != value:
                fail('STALE_INPUT', f'supplied {field} differs from the observed native input')
            item[field] = value
    return result


def remember_candidates(path, result):
    candidates = {r['task']:r['pass_id'] for r in result['results'] if r['sequence']==0}
    if not candidates:
        return
    with _exclusive_lock(path.with_suffix('.lock')):
        record = _load_json(path)
        for alias, candidate in candidates.items():
            prior = record['candidates'].get(alias)
            if prior is not None and prior != candidate:
                fail('STALE_INPUT', 'candidate receipt conflicts with observed native candidate')
            record['candidates'][alias] = candidate
        _write_atomic(path, _json_bytes(record))


def visible_packet(packet):
    result = copy.deepcopy(packet)
    for task in result['tasks']:
        task.pop('input_id', None)
        candidate = task['continuation'].get('candidate')
        if candidate:
            candidate.pop('candidate_pass_id', None)
    return result


def evidence_packet(packet):
    """Exclude only semantic progress; retain every evidence/QA/mapping field."""
    result = copy.deepcopy(packet)
    for task in result['tasks']:
        task.pop('next_sequence', None)
        task.pop('continuation', None)
    return result


def pass_failure(exc, *, semantic=False):
    """Closed action policy: semantic correction is distinct from execution recovery."""
    code = getattr(exc, 'code', 'NATIVE_INTERFACE_ERROR')
    if (semantic and code == 'INVALID_SCHEMA') or code in ('UNINSPECTED_SUPPORT','INVALID_RANGE','OUTSIDE_READING_PACKAGE'):
        state, action, owner = 'draft_rejected', 'correct_semantic_draft', 'worker'
    elif code in ('LOCK_TIMEOUT','LOCK_BUSY'):
        state, action, owner = 'waiting', 'end_worker', 'trusted_operator'
    elif code in ('WORKFLOW_PAUSED','WORKFLOW_STOPPED','CANCELLED','LEASE_EXPIRED'):
        state, action, owner = 'stopped', 'end_worker', 'trusted_operator'
    else:
        state, action, owner = 'recovery_required', 'end_worker', 'trusted_operator'
    return {'ok':False,'code':code,'error':str(exc),'state':state,
        'next_action':action,'recovery_owner':owner,'retry_same_request':False}
