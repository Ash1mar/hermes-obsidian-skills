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
    return hashlib.sha256(b''.join((root/n).read_bytes() for n in ('semantic_output.py','semantic_output_recovery.py','fixed_semantic.py','fixed_semantic_recovery.py','fixed_global_packets.py','fixed_global_targets.py','fixed_global_replay.py','fixed_page_evidence.py','fixed_semantic_schema.py'))).hexdigest()


def fail(message):raise ContractError('RECOVERY_NOT_ALLOWED','$',message)


def uncalled_packet(adapter,binding,value,state,remaining,packet,entry,failure,prefix_journals,prefix_refs):
    """Prove a pre-journal packing rejection; never grant a consumed-call retry."""
    import fixed_global_packets as g
    from fixed_global_targets import target_view,planning_packet
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_schema import schema_for
    if (failure.get('error_code')!='READING_WINDOW_OVERSIZE'
            or failure.get('reason')!='READING_WINDOW_OVERSIZE at $: complete short-target routing exceeds bound; no truncation or call'
            or any(entry.get(k) for k in ('native_audit_ref','attempt_ref','continue_exhausted'))):
        fail('uncalled packet recovery requires the exact pre-journal packing failure')
    short=target_view(packet['view'])
    if entry.get('uncalled_packet_sha256')!=fingerprint(short):fail('uncalled packet identity changed')
    legacy_size=prepare_request_projection('global-plan',packet['view'],schema_for('global-plan',packet['view']),[])['input_size']
    short_size=prepare_request_projection('global-plan',short,schema_for('global-plan',short),[])['input_size']
    if not legacy_size<=g.LIMIT<short_size:fail('the previous full-wire packing defect is not reproduced')
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}"
    if any(path_for(adapter,binding,'global-plan',v).exists() for v in (packet['view'],short)):
        fail('a rejected packing boundary already has an output reservation')
    accounted=set(prefix_refs)
    for jp in prefix_journals:
        if jp.exists():accounted.update(a['audit_ref'] for a in _load_json(jp)['attempts'] if a.get('audit_ref'))
    for jp in (root/'output-attempts').glob('*.json'):
        if jp not in prefix_journals and any(a.get('task_id')==binding['task_id'] for a in _load_json(jp)['attempts']):
            fail('native attempt has an unaccounted reservation or uncertain completion')
    for ap in (root/'semantic-calls').glob('*.json'):
        audit=_load_json(ap)
        if audit.get('native_task_id')==binding['task_id'] and ap.relative_to(adapter.workflow.vault).as_posix() not in accounted:
            fail('native attempt has an unaccounted model call')
    corrected=planning_packet(adapter,binding,value,remaining,state,prepare_request_projection)
    if (path_for(adapter,binding,'global-plan',corrected['view']).exists()
            or not corrected['units'] or corrected['units']!=remaining[:corrected['unit_count']]
            or corrected['input_codepoints']>g.LIMIT):
        fail('corrected packet is consumed, noncontiguous or still oversized')
    return {'previous_view_sha256':fingerprint(short),'previous_proposal_refs':packet['proposal_refs'],
        'previous_codepoints':short_size,'corrected_view_sha256':fingerprint(corrected['view']),
        'corrected_proposal_refs':corrected['proposal_refs'],'corrected_codepoints':corrected['input_codepoints'],
        'preceding_state_sha256':g.state_digest(state),'additional_calls_granted':0,'existing_attempts_changed':False}


def verify(adapter,w,node,binding,entry,failure,runs):
    import fixed_reduce
    import fixed_global_packets as g
    from fixed_semantic import prepare_request_projection,decode_semantic_response
    from fixed_semantic_schema import schema,schema_for
    from fixed_semantic_recovery import transcript_reply,_request_text
    from fixed_global_targets import target_view
    def output_schema(phase,packet):return schema_for(phase,packet['view'])
    def recorded_packet(phase,packet,ref):
        # Schema selection follows the saved native audit, then read() proves
        # the complete wire. This never migrates an unjournalled legacy call.
        audit=_load_json(_vault_path(adapter.workflow.vault,ref))
        def matches(candidate):
            jp=path_for(adapter,binding,phase,candidate['view']);correction=None
            if jp.exists():
                matches=[a for a in _load_json(jp)['attempts'] if a.get('audit_ref')==ref]
                if len(matches)>1:fail('native audit belongs to multiple attempt ordinals')
                if matches:correction=matches[0].get('correction')
            sc=output_schema(phase,candidate)
            return (audit.get('schema_sha256')==fingerprint(sc) and audit.get('input_sha256')==fingerprint(
                prepare_request_projection(phase,candidate['view'],sc,[],correction)['wire']))
        if matches(packet):return packet
        short={**packet,'view':target_view(packet['view'])}
        if matches(short):return short
        if phase=='global-plan':
            measured=g.next_packet(value,remaining,state,prepare_request_projection,view_transform=target_view)
            if matches(measured):return measured
        fail('recorded packet boundary or full output contract changed')
    def legacy_decoder_rejection(phase,packet,draft,diagnostic):
        """Reproduce the old decoder bug, not a relaxed intended output contract."""
        if packet['view'].get('output_contract')!='global-target-refs/v1' or diagnostic.get('code')!='INVALID_SCHEMA':return False
        from hermes_source_units.validation import _check_shape
        try:_check_shape(output_schema(phase,packet),draft,{},'$')
        except ContractError:return False
        try:_check_shape(schema(phase),draft,{},'$')
        except ContractError as exc:return exc.code=='INVALID_SCHEMA'
        return False
    source=_vault_path(adapter.workflow.vault,entry['snapshot_ref']);value=_load_json(source)
    if hashlib.sha256(source.read_bytes()).hexdigest()!=entry['snapshot_sha256'] or value['binding']!=binding:fail('source snapshot or native binding changed')
    if fixed_reduce._snapshot(adapter,binding,{'batch_id':w['batch_id']})!=value:fail('current source/evidence/identity input differs')
    permitted={binding['task_id']:{str(r['id']) for r in runs}}
    for pointer in w.get('execution_recovery_history',[]):
        rp=_vault_path(adapter.workflow.vault,pointer['report_ref'])
        if hashlib.sha256(rp.read_bytes()).hexdigest()!=pointer['report_sha256']:fail('recovery history changed')
        prior=_load_json(rp)
        if prior['request'].get('recovery_scope') not in ('semantic-output/v1','reduce-cancel/v1'):continue
        for old in prior['entries']:
            if old['node']==node.name:permitted[old['previous_task_id']]=set(old['ended_run_ids'])
    def read(ref,phase,packet,seen=None,correction=None):
        path=_vault_path(adapter.workflow.vault,ref);a=_load_json(path)
        seen=set() if seen is None else seen
        if ref in seen:fail('cyclic audit provenance')
        seen.add(ref)
        root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}/semantic-calls"
        if (path.parent!=root or a.get('native_task_id') not in permitted or a.get('node')!=node.name
            or a.get('workflow_id')!=w['workflow_id'] or a.get('phase')!=phase
            or str(a.get('native_run_id')) not in permitted[a['native_task_id']]
            or a.get('template_hash')!=binding['template_hash']):fail('legacy call does not belong to the exact ended native attempt')
        if correction is None:
            jp=path_for(adapter,binding,phase,packet['view'])
            if jp.exists():
                matching=[attempt for attempt in _load_json(jp)['attempts'] if attempt.get('audit_ref')==ref]
                if len(matching)>1:fail('native audit belongs to multiple attempt ordinals')
                if matching:correction=matching[0].get('correction')
        wire=prepare_request_projection(phase,packet['view'],output_schema(phase,packet),[],correction)['wire']
        if a['input_sha256']!=fingerprint(wire) or a['user_sha256']!=fingerprint(wire['user']) or a['schema_sha256']!=fingerprint(output_schema(phase,packet)) or a.get('image_inputs'):fail('legacy full wire/schema/media differs')
        if a.get('reused_from_audit'):
            original,draft,_=read(a['reused_from_audit'],phase,packet,seen,correction)
            if a['response_sha256']!=original['response_sha256']:fail('replay response differs from native original')
            return a,draft,wire
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
    remaining=g.units(value);state=g.initial();pending=[];prefix_journals=set()
    from fixed_global_replay import recorded_packet as replay_packet
    with g.planning_scope(value):
        for ref in saved['successful_call_refs']:
            phase='global-detail' if pending else 'global-plan'
            packet=replay_packet(adapter,binding,value,remaining,state,ref,prepare_request_projection,pending=pending)
            prefix_journals.add(path_for(adapter,binding,phase,packet['view']))
            a,draft,wire=read(ref,phase,packet)
            if a.get('validated') is not True:fail('prior native packet is not accepted')
            state,new_pending=check(phase,value,packet,state,draft)
            if phase=='global-plan':remaining=remaining[packet['unit_count']:];pending=new_pending
            else:pending=pending[1:]
    if state!=saved['state'] or [u['unit'] for u in pending]!=saved['pending_detail_units']:fail('authenticated prior state differs')
    phase='global-detail' if pending else 'global-plan'
    packet=g.detail_packet(value,pending[0],state,prepare_request_projection) if pending else g.next_packet(value,remaining,state,prepare_request_projection)
    if entry.get('uncalled_packet') is True:
        if pending:fail('uncalled packing recovery cannot retry an indivisible detail unit')
        proof=uncalled_packet(adapter,binding,value,state,remaining,packet,entry,failure,prefix_journals,saved['successful_call_refs'])
        return {'uncalled_packet':proof,'input_ref':entry['snapshot_ref'],'progress_ref':entry['progress_ref'],
            'authenticated_prefix_count':len(saved['successful_call_refs'])}
    packet=recorded_packet(phase,packet,entry['native_audit_ref'])
    path=path_for(adapter,binding,phase,packet['view'])
    if entry.get('continue_exhausted') is True:
        for pointer in w.get('execution_recovery_history',[]):
            prior=_load_json(_vault_path(adapter.workflow.vault,pointer['report_ref']))
            if any(e.get('exhausted_continuation',{}).get('journal_name')==path.name for e in prior['entries']):fail('this logical packet already has its explicit continuation authority; resume it without another grant')
        jp=_vault_path(adapter.workflow.vault,entry['attempt_ref'])
        if jp!=path or hashlib.sha256(jp.read_bytes()).hexdigest()!=entry['attempt_sha256']:fail('exhausted attempt record changed')
        journal=_load_json(jp)
        if (journal['contract']!=CONTRACT or journal['input_sha256']!=fingerprint(packet['view'])
            or journal['schema_sha256']!=fingerprint(output_schema(phase,packet)) or len(journal['attempts'])!=2):fail('continuation requires the exact exhausted two-attempt journal')
        prior_correction=None;last_draft=None;last_error=None
        for i,attempt in enumerate(journal['attempts']):
            if (attempt['attempt']!=i+1 or attempt['state']!='rejected' or not attempt.get('correctable')
                or not attempt.get('audit_ref') or attempt.get('correction')!=prior_correction):fail('prior attempts are not two complete, rejected output calls')
            audit,last_draft,wire=read(attempt['audit_ref'],phase,packet,correction=attempt['correction'])
            if audit.get('validated') is not False or attempt['wire_sha256']!=fingerprint(wire):fail('rejected native wire differs')
            try:
                if last_draft is None:raise ContractError('INVALID_SCHEMA','$','native response is not one JSON object')
                check(phase,value,packet,state,last_draft)
            except ContractError as exc:
                if exc.code not in ELIGIBLE or (attempt['diagnostic']['code']!=exc.code and not legacy_decoder_rejection(phase,packet,last_draft,attempt['diagnostic'])):fail('prior rejection is not reproduced by the pure validator')
                last_error=exc
            else:fail('a prior response is now valid; reuse it without another call')
            prior_correction=attempt['diagnostic']
        last=journal['attempts'][-1];bad=_vault_path(adapter.workflow.vault,entry['native_audit_ref'])
        if (entry['native_audit_ref']!=last['audit_ref'] or hashlib.sha256(bad.read_bytes()).hexdigest()!=entry['native_audit_sha256']
            or last['task_id']!=binding['task_id'] or (last_error.code!=failure['error_code'] and not legacy_decoder_rejection(phase,packet,last_draft,{'code':failure['error_code']}))):fail('last failure identity or native audit changed')
        diagnostic=diagnosis(phase,packet,last_draft,last_error,state=state)
        findings=any(diagnostic.get(k) for k in ('page_findings','assigned_and_requested','duplicate_assignments','missing','unknown'))
        if (not findings or diagnostic==last['diagnostic']
            or entry.get('reviewed_diagnostic_sha256')!=fingerprint(diagnostic)):fail('explicit continuation requires reviewed, newly precise mechanical findings')
        if prepare_request_projection(phase,packet['view'],output_schema(phase,packet),[],diagnostic)['input_size']>30000:fail('complete authorized continuation exceeds bound')
        return {'exhausted_continuation':{'journal_name':path.name,'prior_attempts_sha256':fingerprint(journal['attempts']),
            'input_sha256':journal['input_sha256'],'schema_sha256':journal['schema_sha256'],'attempt_ordinal':3,'diagnostic':diagnostic},
            'input_ref':entry['snapshot_ref'],'progress_ref':entry['progress_ref'],'authenticated_prefix_count':len(saved['successful_call_refs'])}
    uncalled=None
    unused=False
    if entry.get('attempt_ref'):
        jp=_vault_path(adapter.workflow.vault,entry['attempt_ref'])
        if jp!=path or hashlib.sha256(jp.read_bytes()).hexdigest()!=entry['attempt_sha256']:fail('attempt record changed')
        journal=_load_json(jp);last=journal['attempts'][-1]
        unused=(failure['error_code']=='READING_WINDOW_OVERSIZE' and len(journal['attempts'])==1
            and last['state']=='rejected' and last.get('correctable') and last.get('audit_ref')==entry['native_audit_ref']
            and last['task_id']==binding['task_id'] and journal['input_sha256']==fingerprint(packet['view'])
            and journal['schema_sha256']==fingerprint(output_schema(phase,packet))
            and prepare_request_projection(phase,packet['view'],output_schema(phase,packet),[],last['diagnostic'])['input_size']>30000)
        if not unused and (failure['error_code']!='INVALID_TRANSITION' or len(journal['attempts'])!=2
            or last.get('session_id') or last.get('audit_ref') or last['task_id']!=binding['task_id']
            or str(last['run_id']) not in permitted[binding['task_id']]
            or last.get('diagnostic',{}).get('code')!='INVALID_TRANSITION'
            or journal['schema_sha256']!=fingerprint(output_schema(phase,packet))):fail('reserved correction is not provably before SDK entry')
        if not unused:uncalled={'journal_name':path.name,'attempt_id':last['attempt_id'],'task_id':last['task_id']}
    bad_path=_vault_path(adapter.workflow.vault,entry['native_audit_ref'])
    if hashlib.sha256(bad_path.read_bytes()).hexdigest()!=entry['native_audit_sha256']:fail('failed native audit changed')
    a,draft,wire=read(entry['native_audit_ref'],phase,packet)
    if a.get('validated') is not False:fail('legacy import requires a recorded rejected response')
    try:
        if draft is None:raise ContractError('INVALID_SCHEMA','$','native response is not one JSON object')
        check(phase,value,packet,state,draft)
    except ContractError as exc:
        if exc.code not in ELIGIBLE or (not uncalled and not unused and exc.code!=failure['error_code']):fail('failure is not reproduced at the pure output validator')
        diagnostic=diagnosis(phase,packet,draft,exc,state=state)
    else:fail('legacy response no longer reproduces a contract rejection')
    if path.exists() and len(_load_json(path)['attempts'])>=2 and not uncalled:fail('the shared correction allowance is already consumed')
    if prepare_request_projection(phase,packet['view'],output_schema(phase,packet),[],diagnostic)['input_size']>30000:fail('complete correction request exceeds bound')
    if uncalled:
        diagnostic=last['correction']  # Preserve the already reserved exact wire.
        if journal['attempts'][0]['audit_ref']!=entry['native_audit_ref'] or last['wire_sha256']!=fingerprint(prepare_request_projection(phase,packet['view'],output_schema(phase,packet),[],diagnostic)['wire']):fail('reserved correction no longer matches its original failure and diagnostic')
        return {'uncalled_reservation':uncalled,'input_ref':entry['snapshot_ref'],'progress_ref':entry['progress_ref'],'authenticated_prefix_count':len(saved['successful_call_refs'])}
    if unused:
        return {'output_seed':{'journal_name':path.name,'record':journal},'input_ref':entry['snapshot_ref'],
            'progress_ref':entry['progress_ref'],'authenticated_prefix_count':len(saved['successful_call_refs']),
            'unused_correction':{'next_ordinal':2,'measured_codepoints':prepare_request_projection(phase,packet['view'],output_schema(phase,packet),[],diagnostic)['input_size']}}
    return {'output_seed':{'journal_name':path.name,'record':{'contract':CONTRACT,'phase':phase,'input_sha256':fingerprint(packet['view']),
        'schema_sha256':fingerprint(output_schema(phase,packet)),'attempts':[{'attempt':1,'attempt_id':fingerprint({'path':path.name,'attempt':1}),
        'task_id':binding['task_id'],'run_id':a['native_run_id'],'wire_sha256':fingerprint(wire),'audit_ref':entry['native_audit_ref'],
        'state':'rejected','correctable':True,'diagnostic':diagnostic,'correction':None}]}},
        'input_ref':entry['snapshot_ref'],'progress_ref':entry['progress_ref'],'authenticated_prefix_count':len(saved['successful_call_refs'])}
