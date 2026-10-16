"""One reviewed source-hold recovery, using immutable native evidence and exact packets."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.validation import fingerprint

SCOPE = 'source-hold/v1'


def fail(message):
    raise ContractError('RECOVERY_NOT_ALLOWED', '$', message)


def contract_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def source_identity(snapshot):
    """The batch-wide loss counter is not source evidence; everything else is exact."""
    view = copy.deepcopy(snapshot['view'])
    limits = view.get('coverage_limitations', {})
    limits.pop('excluded_task_count', None)
    backend = {k:v for k,v in snapshot['backend'].items() if k != 'coverage_gap_reports'}
    return fingerprint({'view':view, 'backend':backend})


def original(vault, entry):
    prefix = '_system/ledgers/ingest-workflows/'
    if not entry['snapshot_ref'].startswith(prefix) or '/semantic-inputs/' not in entry['snapshot_ref']:
        fail('original snapshot must belong to a workflow')
    path = _vault_path(vault, entry['snapshot_ref'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry['snapshot_sha256']:
        fail('original source snapshot changed')
    value = _load_json(path)
    if fingerprint({k:v for k,v in value.items() if k != 'input_id'}) != value['input_id']:
        fail('original source snapshot fingerprint differs')
    return value


def entries(workflow, vault, node):
    for pointer in reversed(workflow.get('execution_recovery_history', [])):
        report = _load_json(_vault_path(vault, pointer['report_ref']))
        if report['request'].get('recovery_scope') == SCOPE:
            for entry in report['entries']:
                if entry['node'] == node:
                    yield entry


def rebind_view(adapter, workflow, binding, snapshot):
    """Preserve exact old request bytes only when this source's full identity is unchanged.

    The old batch counter remains historical request metadata. Current coverage reports
    stay in the backend and downstream global input; no source exclusions are removed.
    """
    entry = next(entries(workflow, adapter.workflow.vault, binding['node']), None)
    if entry is None:
        return snapshot
    old = original(adapter.workflow.vault, entry)
    if source_identity(snapshot) != entry['source_identity'] or source_identity(old) != entry['source_identity']:
        fail('source tasks, candidates, evidence, QA or source exclusions changed')
    snapshot['view'] = copy.deepcopy(old['view'])
    return snapshot


def presence(view):
    result = {}
    packet = view['original_evidence']
    for task in packet['tasks']:
        refs = []
        for material in task['materials']:
            if material.get('text_ref') and material['text_ref'] not in packet.get('texts', {}):
                fail('material text handle is absent')
            if material.get('qa_ref') and material['qa_ref'] not in packet.get('qa', {}):
                fail('material QA handle is absent')
            refs.append(material['ref'])
        result[task['task']] = refs
    for candidate in view['candidates']:
        for link in candidate['source_evidence']:
            if not set(link['support_refs']) <= set(result.get(link['reading'], [])):
                fail('candidate support is absent from its own original reading')
    return result


def resolved_view(adapter, binding, view):
    workflow = adapter.workflow.status(binding['workflow_id'])
    entry = next(entries(workflow, adapter.workflow.vault, binding['node']), None)
    if entry is None or entry.get('blocked_view_sha256') != fingerprint(view):
        return view
    # Only this previously refused packet changes. Successful packets retain exact bytes.
    return clarify(view)


def clarify(view):
    return {**view, 'hold_resolution': {
        'materials_present': presence(view),
        'rule': 'This index proves presence only, not semantic correctness. Inspect the supplied original text/images and QA. '
                'If a candidate cannot be verified because of recorded source QA, explicitly omit that candidate using '
                'omitted_candidate_refs and explain the coverage loss in reason. Preserve other verifiable candidates. '
                'Do not repair source text, infer missing facts, or describe omissions as verified coverage. '
                'A genuinely missing required asset or invalid binding must still block.'}}


def transport_messages(rows, entry):
    """Connection logs must independently prove transport, never infer it from prose."""
    users=[c for r,c,_ in rows if r=='user'];answers=[c for r,c,_ in rows if r=='assistant']
    unprocessed='Your request was not processed. Send it again if you still want me to carry it out.'
    if (len(users)!=1 or len(answers)!=1 or any(t and t!='[]' for _,_,t in rows)
            or hashlib.sha256(users[0].encode()).hexdigest()!=entry['transport_user_sha256']
            or hashlib.sha256(answers[0].encode()).hexdigest()!=entry['transport_response_sha256']
            or not ('Connection error' in answers[0] or answers[0].strip()==unprocessed)):
        fail('transport failure transcript differs')
    return users[0]


def verify(adapter, workflow, node, binding, entry, failure, runs, *, home=None):
    from fixed_reduce import _snapshot
    from fixed_reduce_packets import plan
    from fixed_semantic import prepare_request_projection, decode_semantic_response
    from fixed_semantic_schema import schema
    from revision_disposition import native_user
    from fixed_semantic_recovery import _request_text
    old = original(adapter.workflow.vault, entry)
    if old['binding'] != binding:
        fail('original snapshot belongs to a different binding')
    current = _snapshot(adapter, binding, {'batch_id':workflow['batch_id']})
    identity = source_identity(current)
    if identity != source_identity(old):
        fail('current source inputs differ from the ended native attempt')
    if not any(r.get('worker_pid') and r.get('ended_at') for r in runs):
        fail('recovery requires an ended actual native attempt')
    proof = {'snapshot_ref':entry['snapshot_ref'], 'snapshot_sha256':entry['snapshot_sha256'],
             'source_identity':identity, 'historical_batch_counter_only':old['view'] != current['view']}
    home = Path(home) if home is not None else Path.home()/'.hermes'
    with sqlite3.connect('file:'+str(home/'state.db')+'?mode=ro', uri=True) as db:
        if failure['error_code'] == 'SEMANTIC_REVIEW_REQUIRED':
            ref = entry['native_audit_ref']
            prefix = f'_system/ledgers/ingest-workflows/{workflow["workflow_id"]}/semantic-calls/'
            if not ref.startswith(prefix) or '/' in ref[len(prefix):]:
                fail('native refusal audit must belong to this workflow')
            path = _vault_path(adapter.workflow.vault, ref); audit = _load_json(path)
            if (hashlib.sha256(path.read_bytes()).hexdigest() != entry['native_audit_sha256']
                    or audit.get('workflow_id') != workflow['workflow_id'] or audit.get('node') != node.name
                    or audit.get('native_task_id') != binding['task_id'] or audit.get('reused_from_audit')
                    or str(audit.get('native_run_id')) not in {str(r['id']) for r in runs}
                    or audit.get('template_hash') != binding['template_hash']
                    or audit.get('phase') != 'resource-reduce'
                    or audit.get('schema_sha256') != fingerprint(schema('resource-reduce'))
                    or audit.get('model', {}).get('origin') != 'hermes_native_model'):
                fail('refusal audit identity differs')
            session = audit['model']['session_id']
            rows = list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?', (session,)))
            users = [u for role,c,_ in rows if role == 'user' and (u:=native_user(c,audit)) is not None]
            responses = [c for role,c,_ in rows if role == 'assistant' and fingerprint(c) == audit['response_sha256']]
            if (db.execute('SELECT source FROM sessions WHERE id=?',(session,)).fetchone() != ('kanban',)
                    or len(users) != 1 or len(responses) != 1 or any(t and t != '[]' for _,_,t in rows)
                    or audit['model'].get('response_sha256') != audit['response_sha256']):
                fail('native refusal transcript differs')
            draft,_ = decode_semantic_response(responses[0])
            if not draft.get('blocked_reason') or draft['blocked_reason'] not in failure['reason']:
                fail('selected audit does not explain the source hold')
            body,_ = json.JSONDecoder().raw_decode(_request_text(users[0]))
            matches = [p for p in plan(adapter, old, prepare_request_projection) if p['view'] == body.get('input')]
            if len(matches) != 1 or body.get('phase') != 'resource-reduce':
                fail('refused packet differs from current complete source evidence')
            packet = matches[0]; presence(packet['view'])
            from fixed_semantic_media import image_catalog, select_images
            images=select_images(packet['view']['original_evidence']['tasks'],
                                 image_catalog(adapter,packet['view']['original_evidence']))
            prepared=prepare_request_projection('resource-reduce',packet['view'],schema('resource-reduce'),images)
            if (fingerprint(prepared['wire']) != audit['input_sha256']
                    or prepared['wire']['user'] != users[0]
                    or [{k:v for k,v in i.items() if k!='url'} for i in images] != audit.get('image_inputs',[])):
                fail('native system, user, schema or registered images changed')
            proof.update(native_audit_ref=ref, native_audit_sha256=entry['native_audit_sha256'],
                         blocked_view_sha256=fingerprint(packet['view']))
            preview=clarify(packet['view'])
            if prepare_request_projection('resource-reduce',preview,schema('resource-reduce'),images)['input_size'] > 30000:
                fail('complete clarified packet exceeds the bound')
        elif failure['error_code'] == 'PROGRAM_EXECUTION_FAILED':
            session = entry['transport_session_id']
            log = adapter.kanban.task_log(workflow['kanban']['board_id'], binding['task_id'])
            if session not in log or 'APIConnectionError' not in log or 'Connection error' not in log:
                fail('native run log does not prove this transport failure')
            if db.execute('SELECT source FROM sessions WHERE id=?',(session,)).fetchone() != ('kanban',):
                fail('transport session is not native')
            rows = list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?',(session,)))
            user=transport_messages(rows,entry)
            body,_=json.JSONDecoder().raw_decode(_request_text(user))
            if body.get('phase')!='resource-reduce' or not any(
                    p['view']==body.get('input') for p in plan(adapter,old,prepare_request_projection)):
                fail('failed transport request belongs to different source evidence')
            proof.update(transport_session_id=session, native_log_sha256=hashlib.sha256(log.encode()).hexdigest(),
                         transport_response_sha256=entry['transport_response_sha256'])
        else:
            fail('only reviewed source refusal or proven transport failure may recover')
    return proof
