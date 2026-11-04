"""Explicit stopped-operator quarantine; unknown calls never become retry grants."""
import hashlib
import json
from pathlib import Path
import sqlite3
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.validation import fingerprint


def fail(message):
    raise ContractError('SKIP_NOT_ALLOWED', '$', message)


def verify(adapter, value, item, failure, entry):
    if entry.get('deferral') == 'automatic_unknown_quarantine':
        from hermes_source_units.recovery_policy import effective
        from orchestration import configuration
        if (value['scope'].get('execution_mode') != 'auto_full'
                or effective(value,configuration())['unknown_completion'] != 'isolate_after_drain'
                or (value['dispatch_policy']['mode']=='canary'
                    and item['slice_id'] not in value['dispatch_policy']['slice_ids'])):
            fail('automatic unknown isolation is outside the effective workflow policy')
    if entry.get('acknowledge_unverified_coverage') is not True:
        fail('unknown isolation requires explicit acknowledgement of lost coverage')
    if failure.get('error_code') != 'NATIVE_COMPLETION_UNKNOWN':
        fail('unknown isolation requires a structured native completion failure')
    node = 'pass-slice:' + item['slice_id']
    card = next((c for c in value['kanban']['task_map'] if c['node'] == node), None)
    if not card or failure.get('node') != node or failure.get('task_id') != card['task_id']:
        fail('unknown isolation binding differs')
    snap = adapter.kanban.task_snapshot(value['kanban']['board_id'], card['task_id'])
    runs = [r for r in snap.get('runs', []) if r.get('worker_pid')]
    if (not runs or any(not r.get('ended_at') for r in snap.get('runs', []))
            or snap.get('task', {}).get('worker_pid') or snap.get('task', {}).get('current_run_id')):
        fail('unknown isolation requires ended native ownership')
    evidence = entry.get('unknown_call', {})
    path = _vault_path(adapter.workflow.vault, evidence.get('ref', ''))
    root = adapter.workflow.vault / '_system/ledgers/ingest-workflows' / value['workflow_id']
    if path.parent != root / 'call-failures' or path.suffix != '.json':
        fail('unknown call evidence must belong to this workflow')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != evidence.get('sha256'): fail('unknown call evidence changed')
    call = json.loads(data)
    prefix = 'NATIVE_COMPLETION_UNKNOWN at $: '
    try:
        detail = json.loads(failure['reason'][len(prefix):]) if failure['reason'].startswith(prefix) else {}
    except ValueError:
        detail = {}
    if (detail.get('report') != str(path) or detail.get('session_id') != call.get('session_id')
            or call.get('contract') != 'native-call-outcome/v1' or call.get('state') != 'completion_unknown'
            or call.get('task_id') != card['task_id'] or str(call.get('run_id')) not in {str(r['id']) for r in runs}
            or call.get('completed_output') is not False or call.get('extra_calls_granted') != 0
            or call.get('phase') not in ('candidate','citation')
            or fingerprint(call.get('wire')) != call.get('wire_sha256')):
        fail('unknown call identity or immutable outcome differs')
    from revision_disposition import native_user
    audit = {'user_sha256': fingerprint(call['wire']['user']), 'image_inputs': call.get('image_inputs', [])}
    with sqlite3.connect('file:' + str(Path.home()/'.hermes/state.db') + '?mode=ro', uri=True) as db:
        if db.execute('SELECT source FROM sessions WHERE id=?', (call['session_id'],)).fetchone() != ('kanban',):
            fail('unknown call lacks native session identity')
        rows = list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?', (call['session_id'],)))
    if (any(t and t != '[]' for _,_,t in rows)
            or [native_user(c,audit) for r,c,_ in rows if r == 'user'].count(call['wire']['user']) != 1):
        fail('unknown native request transcript differs')
    if any(r == 'assistant' and c for r,c,_ in rows):
        fail('a returned response must be authenticated before considering quarantine')
    if any(_load_json(p).get('model', {}).get('session_id') == call['session_id']
           for p in (root/'semantic-calls').glob('*.json')):
        fail('a durable response exists; unknown isolation cannot replace its verification')
    budget = _load_json(root/'pass-call-budget.json')
    reserved = {r['task_id'] for r in budget['tasks'].values() if r['phase'] == call['phase']
                and any(a.get('native_task_id') == card['task_id'] and a['state'] == 'reserved' for a in r['attempts'])}
    pending = {t for t in item['task_ids'] if not adapter.workflow._task_passes_complete(t)}
    selected = set(entry.get('task_ids', item['task_ids']))
    if not reserved or reserved != pending or selected != pending:
        fail('unknown reservation must match the exact unfinished tasks; completed tasks are preserved')
    return {'contract':'pass-unknown-quarantine/v1','session_id':call['session_id'],
            'native_task_id':card['task_id'],'native_run_id':call['run_id'],
            'call_ref':evidence['ref'],'call_sha256':evidence['sha256'],
            'task_ids':[t for t in item['task_ids'] if t in pending],'original_outcome':'completion_unknown',
            'reserved_budget_preserved':True,'new_calls_granted':0,'verification_success':False}


def automatic_entry(adapter, value, item, failure):
    """Select authenticated stopped unknown work without another model call."""
    prefix='NATIVE_COMPLETION_UNKNOWN at $: '
    reason=failure.get('reason','')
    if not reason.startswith(prefix):fail('structured unknown outcome is required')
    try:path=Path(json.loads(reason[len(prefix):])['report'])
    except (ValueError,KeyError,TypeError):fail('unknown report reference is invalid')
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{value["workflow_id"]}/call-failures'
    if path.parent!=root:fail('unknown report belongs to another workflow')
    entry={'slice_id':item['slice_id'],'deferral':'automatic_unknown_quarantine',
           'acknowledge_unverified_coverage':True,
           'task_ids':[t for t in item['task_ids'] if not adapter.workflow._task_passes_complete(t)],
           'unknown_call':{'ref':path.relative_to(adapter.workflow.vault).as_posix(),
                           'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},
           'reason':'Program quarantine of stopped uncertain execution; no retry, budget reset or verified coverage claim'}
    return entry, verify(adapter,value,item,failure,entry)
