"""Authenticate legacy native output failures into the shared two-attempt journal.

This is one migration/admission path, not a semantic retry by error name.
"""
import hashlib,json
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _load_json,_vault_path
from semantic_output import SCOPE,CONTRACT,ELIGIBLE,check,diagnosis,path_for


def contract_hash():
    root=Path(__file__).resolve().parent
    return hashlib.sha256(b''.join((root/n).read_bytes() for n in ('semantic_output.py','semantic_output_recovery.py','fixed_semantic.py','fixed_semantic_recovery.py','fixed_global_packets.py','fixed_semantic_schema.py'))).hexdigest()


def fail(message):raise ContractError('RECOVERY_NOT_ALLOWED','$',message)


def verify(adapter,w,node,binding,entry,failure,runs):
    import fixed_reduce
    import fixed_global_packets as g
    from fixed_semantic import prepare_request_projection,decode_semantic_response
    from fixed_semantic_schema import schema
    from fixed_semantic_recovery import transcript_reply,_request_text
    source=_vault_path(adapter.workflow.vault,entry['snapshot_ref']);value=_load_json(source)
    if hashlib.sha256(source.read_bytes()).hexdigest()!=entry['snapshot_sha256'] or value['binding']!=binding:fail('source snapshot or native binding changed')
    if fixed_reduce._snapshot(adapter,binding,{'batch_id':w['batch_id']})!=value:fail('current source/evidence/identity input differs')
    def read(ref,phase,packet):
        path=_vault_path(adapter.workflow.vault,ref);a=_load_json(path)
        root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}/semantic-calls"
        if (path.parent!=root or a.get('native_task_id')!=binding['task_id'] or a.get('node')!=node.name
            or a.get('workflow_id')!=w['workflow_id'] or a.get('phase')!=phase or a.get('reused_from_audit')
            or str(a.get('native_run_id')) not in {str(r['id']) for r in runs}
            or a.get('template_hash')!=binding['template_hash']):fail('legacy call does not belong to the exact ended native attempt')
        wire=prepare_request_projection(phase,packet['view'],schema(phase),[])['wire']
        if a['input_sha256']!=fingerprint(wire) or a['user_sha256']!=fingerprint(wire['user']) or a['schema_sha256']!=fingerprint(schema(phase)) or a.get('image_inputs'):fail('legacy full wire/schema/media differs')
        raw=transcript_reply(a,wire)
        if raw is None:fail('legacy native transcript does not authenticate')
        try:draft,_=decode_semantic_response(raw)
        except json.JSONDecodeError as exc:draft=None
        return a,draft,wire
    # Reconstruct the prior state from authenticated successful native packets,
    # never from a model-authored or merely serialized state assertion.
    if node.kind!='global-reduce':fail('legacy journal import currently requires incremental global native provenance')
    progress=_vault_path(adapter.workflow.vault,entry['progress_ref']);saved=_load_json(progress)
    if hashlib.sha256(progress.read_bytes()).hexdigest()!=entry['progress_sha256'] or saved['binding']!=binding or saved['input_ref']!=entry['snapshot_ref']:fail('progress binding changed')
    remaining=g.units(value);state=g.initial();pending=[]
    for ref in saved['successful_call_refs']:
        phase='global-detail' if pending else 'global-plan'
        packet=g.detail_packet(value,pending[0],state,prepare_request_projection) if pending else g.next_packet(value,remaining,state,prepare_request_projection)
        a,draft,wire=read(ref,phase,packet)
        if a.get('validated') is not True:fail('prior native packet is not accepted')
        state,new_pending=check(phase,value,packet,state,draft)
        if phase=='global-plan':remaining=remaining[packet['unit_count']:];pending=new_pending
        else:pending=pending[1:]
    if state!=saved['state'] or [u['unit'] for u in pending]!=saved['pending_detail_units']:fail('authenticated prior state differs')
    phase='global-detail' if pending else 'global-plan'
    packet=g.detail_packet(value,pending[0],state,prepare_request_projection) if pending else g.next_packet(value,remaining,state,prepare_request_projection)
    bad_path=_vault_path(adapter.workflow.vault,entry['native_audit_ref'])
    if hashlib.sha256(bad_path.read_bytes()).hexdigest()!=entry['native_audit_sha256']:fail('failed native audit changed')
    a,draft,wire=read(entry['native_audit_ref'],phase,packet)
    if a.get('validated') is not False:fail('legacy import requires a recorded rejected response')
    try:
        if draft is None:raise ContractError('INVALID_SCHEMA','$','native response is not one JSON object')
        check(phase,value,packet,state,draft)
    except ContractError as exc:
        if exc.code not in ELIGIBLE or exc.code!=failure['error_code']:fail('failure is not reproduced at the pure output validator')
        diagnostic=diagnosis(phase,packet,draft,exc)
    else:fail('legacy response no longer reproduces a contract rejection')
    path=path_for(adapter,binding,phase,packet['view'])
    if path.exists() and len(_load_json(path)['attempts'])>=2:fail('the shared correction allowance is already consumed')
    if prepare_request_projection(phase,packet['view'],schema(phase),[],diagnostic)['input_size']>30000:fail('complete correction request exceeds bound')
    return {'output_seed':{'journal_name':path.name,'record':{'contract':CONTRACT,'phase':phase,'input_sha256':fingerprint(packet['view']),
        'schema_sha256':fingerprint(schema(phase)),'attempts':[{'attempt':1,'attempt_id':fingerprint({'path':path.name,'attempt':1}),
        'task_id':binding['task_id'],'run_id':a['native_run_id'],'wire_sha256':fingerprint(wire),'audit_ref':entry['native_audit_ref'],
        'state':'rejected','correctable':True,'diagnostic':diagnostic,'correction':None}]}},
        'input_ref':entry['snapshot_ref'],'progress_ref':entry['progress_ref'],'authenticated_prefix_count':len(saved['successful_call_refs'])}
