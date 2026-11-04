"""Authenticate returned requests after a budget lock failure; never resend unknowns."""
import hashlib
import json
from pathlib import Path
import sqlite3

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _exclusive_lock, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint

SCOPE = 'pass-budget-lock/v1'


def fail(message):
    raise ContractError('RECOVERY_NOT_ALLOWED', '$', message)


def verify(adapter, value, binding, failure, snapshot):
    root = adapter.workflow.vault / '_system/ledgers/ingest-workflows' / value['workflow_id']
    budget = root / 'pass-call-budget.json'
    expected = 'LOCK_BUSY at $: Skill kernel lock is held: ' + str(budget.with_suffix('.lock'))
    if failure.get('error_code') != 'LOCK_BUSY' or not failure.get('reason', '').startswith(expected + '; wait limit='):
        fail('only the exact workflow budget lock failure is recoverable')
    runs = [r for r in snapshot.get('runs', []) if r.get('worker_pid')]
    if not runs or any(not r.get('ended_at') for r in runs) or snapshot.get('task', {}).get('worker_pid'):
        fail('budget recovery requires ended native consumers')
    if any(binding['node'] in p['nodes'] for p in value.get('execution_recovery_history', [])
           if _load_json(adapter.workflow.vault / p['report_ref'])['request'].get('recovery_scope') == SCOPE):
        fail('the one budget lock recovery has already been consumed')
    from hermes_source_units.runtime_telemetry import runtime_directory
    events = []
    for run in runs:
        path = runtime_directory(adapter.workflow.vault, value['workflow_id']) / f"{run['worker_pid']}.jsonl"
        if not path.is_file(): fail('native request timeline is missing')
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows = [e for e in rows if e.get('workflow_id') == value['workflow_id'] and e.get('task_id') == binding['task_id']]
        if not any(e['name'] == 'worker_registered' and str(e.get('native_run_id')) == str(run['id']) for e in rows):
            fail('native registration is not authenticated')
        events.extend(rows)
    starts = {e['request_id'] for e in events if e['name'] == 'sdk_request_start'}
    ends = {e['request_id'] for e in events if e['name'] == 'sdk_model_request' and not e.get('error')}
    if starts != ends: fail('native request completion remains unknown')
    audits = [(p, _load_json(p)) for p in (root / 'semantic-calls').glob('*.json')]
    audits = [(p,a) for p,a in audits if a.get('native_task_id') == binding['task_id'] and not a.get('reused_from_audit')]
    if {a.get('request_id') for _,a in audits} != starts or len(audits) != len(starts):
        fail('every actual request must have exactly one durable audit')
    from fixed_semantic import COMMON, RULES
    from fixed_semantic_recovery import _request_text, task_content, transcript_reply
    from revision_disposition import native_user
    ledger = _load_json(budget) if budget.exists() else {'tasks': {}}
    proofs = {}; authenticated = []
    home = Path.home() / '.hermes'
    with sqlite3.connect('file:' + str(home / 'state.db') + '?mode=ro', uri=True) as db:
        for path, audit in audits:
            if str(audit.get('native_run_id')) not in {str(r['id']) for r in runs}: fail('audit run identity differs')
            phase = audit['phase']
            if phase not in ('candidate', 'citation'): fail('unexpected request phase')
            users = [native_user(c,audit) for (c,) in db.execute('SELECT content FROM messages WHERE session_id=? AND role=?',
                     (audit['model']['session_id'], 'user'))]
            users = [u for u in users if u is not None and fingerprint(u) == audit['user_sha256']]
            if len(users) != 1: fail('native request transcript differs')
            wire = {'system': COMMON + '\n' + RULES[phase], 'user': users[0]}
            if fingerprint(wire) != audit['input_sha256'] or transcript_reply(audit, wire, home=home) is None:
                fail('native response or request cannot be authenticated')
            body, _ = json.JSONDecoder().raw_decode(_request_text(users[0]))
            if body['phase'] != phase: fail('phase identity differs')
            view = body['input']; ref = path.relative_to(adapter.workflow.vault).as_posix()
            authenticated.append({'ref': ref, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            for task in view['tasks']:
                tid = audit['task_identities'][task['task']]
                key = fingerprint({'node': binding['node'], 'task': tid, 'phase': phase, 'input': task_content(view, task)})
                row = ledger['tasks'].get(key)
                if not row: fail('native call has no durable reservation')
                reserved = [a for a in row['attempts'] if a.get('native_task_id') == binding['task_id'] and a['state'] == 'reserved']
                if reserved:
                    if len(reserved) != 1 or key in proofs: fail('ambiguous budget completion')
                    proofs[key] = ref
                elif not any(a.get('audit_ref') == ref and a['state'] == 'returned' for a in row['attempts']):
                    fail('call does not match its consumed budget')
    unresolved = {k for k,r in ledger['tasks'].items() if any(a.get('native_task_id') == binding['task_id'] and a['state'] == 'reserved' for a in r['attempts'])}
    if unresolved != set(proofs): fail('unconfirmed reservations remain held')
    return {'contract': SCOPE, 'settlements': proofs, 'audits': authenticated,
            'native_task_id': binding['task_id'], 'request_count': len(starts)}


def settle(adapter, wid, proof):
    path = adapter.workflow.vault / '_system/ledgers/ingest-workflows' / wid / 'pass-call-budget.json'
    if not proof['settlements']: return
    with _exclusive_lock(path.with_suffix('.lock'), timeout=5):
        ledger = _load_json(path)
        for key, ref in proof['settlements'].items():
            row = ledger['tasks'][key]['attempts'][-1]
            if row.get('native_task_id') != proof['native_task_id']: fail('budget owner changed')
            if row['state'] == 'returned' and row.get('audit_ref') == ref: continue
            if row['state'] != 'reserved': fail('budget changed during recovery')
            row.update(state='returned', audit_ref=ref)
        _write_atomic(path, _json_bytes(ledger))
