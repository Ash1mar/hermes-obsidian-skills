"""One reviewed source-hold recovery, using immutable native evidence and exact packets."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.validation import fingerprint

SCOPE = 'source-hold/v3'
SCOPES = ('source-hold/v1', 'source-hold/v2', SCOPE)


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
        if report['request'].get('recovery_scope') in SCOPES:
            for entry in report['entries']:
                if entry['node'] == node:
                    yield {**entry, '_recovery_scope':report['request']['recovery_scope']}


def rebind_view(adapter, workflow, binding, snapshot):
    """Preserve exact old request bytes only when this source's full identity is unchanged.

    The old batch counter remains historical request metadata. Current coverage reports
    stay in the backend and downstream global input; no source exclusions are removed.
    """
    entry = next(entries(workflow, adapter.workflow.vault, binding['node']), None)
    if entry is None:
        return snapshot
    from citation_amendment_compat import source_rebind
    if source_rebind(adapter,workflow,binding,snapshot,entry):return snapshot
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
            handles=[r if isinstance(r,str) else r.get('ref') if isinstance(r,dict) else None
                     for r in link['support_refs']]
            if any(not isinstance(r,str) for r in handles) or not set(handles) <= set(result.get(link['reading'], [])):
                fail('candidate support is absent from its own original reading')
    return result


def resolved_view(adapter, binding, view):
    workflow = adapter.workflow.status(binding['workflow_id'])
    entry = next(entries(workflow, adapter.workflow.vault, binding['node']), None)
    if entry is None:
        return view
    # Called only after exact successful packet replay misses. No accepted call
    # is repeated just to acquire the clarified disposition instructions.
    return clarify(view)


def clarify(view):
    return {**view, 'hold_resolution': {
        'materials_present': presence(view),
        'rule': 'This index proves presence only, not semantic correctness. Inspect each candidate against its own supplied original text/images and QA. '
                'If the inspected material does not verify a candidate, explicitly omit that candidate using '
                'omitted_candidate_refs and explain the coverage loss in reason. Preserve other verifiable candidates. '
                'Do not repair source text, infer missing facts, or describe omissions as verified coverage. '
                'A genuinely missing required asset or invalid binding must still block.'}}


def resolved_audit_view(adapter,binding,view):
    """Keep v2 exact-success replay before clarifying the earlier audit's role."""
    if view.get('hold_resolution',{}).get('audit_role'):
        return view
    workflow=adapter.workflow.status(binding['workflow_id'])
    entry=next(entries(workflow,adapter.workflow.vault,binding['node']),None)
    if entry is None or entry['_recovery_scope']!=SCOPE:
        return view
    return clarify_audit(view)


def clarify_audit(view):
    # Validate local evidence links, without repeating the material index already
    # present in original_evidence. The complete disposition rule is in system.
    presence(view)
    return {**view,'hold_resolution':{
        'audit_role':'prior-review-not-source',
        'rule':'Prior review is metadata; retain QA.'}}


def unclarified_view(view, authorized_view_sha256):
    """Authenticate old program framing separately; every evidence field stays exact."""
    if 'hold_resolution' not in view:
        return view
    base={k:v for k,v in view.items() if k!='hold_resolution'}
    marker=view['hold_resolution']
    authorized=({authorized_view_sha256} if isinstance(authorized_view_sha256,str)
                else set(authorized_view_sha256 or []))
    if (fingerprint(base) not in authorized or not isinstance(marker,dict)
            or set(marker) not in ({'materials_present','rule'},{'materials_present','rule','audit_role'},{'rule','audit_role'})
            or ('audit_role' in marker and marker['audit_role']!='prior-review-not-source') or not isinstance(marker['rule'],str)
            or not marker['rule'].strip() or ('materials_present' in marker and marker['materials_present']!=presence(base))):
        fail('old clarification lacks its authenticated packet and exact material index')
    presence(base)
    return base


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


def verify(adapter, workflow, node, binding, entry, failure, runs, *, home=None, syntax_only=False):
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
            if syntax_only:
                try:decode_semantic_response(responses[0])
                except json.JSONDecodeError:pass
                else:fail('format recovery requires an actually undecodable original response')
                if not any(_load_json(_vault_path(adapter.workflow.vault,r)).get('code')=='INVALID_SCHEMA'
                           for r in audit.get('receipt_refs',[])):
                    fail('format failure lacks its native schema rejection')
                if 'semantic response is not one JSON object' not in failure['reason']:
                    fail('format rejection does not explain current source hold')
            else:
                draft,_ = decode_semantic_response(responses[0])
                if not draft.get('blocked_reason') or draft['blocked_reason'] not in failure['reason']:
                    fail('selected audit does not explain the source hold')
            body,_ = json.JSONDecoder().raw_decode(_request_text(users[0]))
            prior=next(entries(workflow,adapter.workflow.vault,node.name),None)
            planned=plan(adapter,old,prepare_request_projection)
            authorized=( {fingerprint(p['view']) for p in planned}
                if prior and prior['_recovery_scope'] in ('source-hold/v2',SCOPE) and prior['source_identity']==identity
                else prior.get('blocked_view_sha256') if prior else None)
            base=unclarified_view(body['input'],authorized)
            matches = [p for p in planned if p['view'] == base]
            if len(matches) != 1 or body.get('phase') != 'resource-reduce':
                fail('refused packet differs from current complete source evidence')
            packet = matches[0]; presence(packet['view'])
            from fixed_semantic_media import image_catalog, select_images
            images=select_images(packet['view']['original_evidence']['tasks'],
                                 image_catalog(adapter,packet['view']['original_evidence']))
            prepared=prepare_request_projection('resource-reduce',body['input'],schema('resource-reduce'),images)
            wires=[prepared['wire']]
            if 'hold_resolution' in body['input'] and prior and prior['_recovery_scope']=='source-hold/v1':
                base_wire=prepare_request_projection('resource-reduce',base,schema('resource-reduce'),images)['wire']
                wires.append({**prepared['wire'],'system':base_wire['system']})
            if (audit['input_sha256'] not in {fingerprint(w) for w in wires}
                    or prepared['wire']['user'] != users[0]
                    or [{k:v for k,v in i.items() if k!='url'} for i in images] != audit.get('image_inputs',[])):
                fail('native system, user, schema or registered images changed')
            proof.update(native_audit_ref=ref, native_audit_sha256=entry['native_audit_sha256'],
                         blocked_view_sha256=fingerprint(packet['view']))
            preview=clarify_audit(packet['view'])
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
