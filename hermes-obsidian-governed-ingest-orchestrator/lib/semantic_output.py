"""One durable correction for mechanically identified fixed-output errors.

This journal controls attempts, never edits semantic answers. Unknown in-flight
calls and material/authority/transport failures remain held.
"""
import copy,json,os
from collections import Counter
from contextvars import ContextVar
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint
from fixed_semantic_schema import schema, schema_for

SCOPE='semantic-output/v1'
CONTRACT='finite-semantic-output/v1'
ACTIVE=ContextVar('semantic_output_attempt',default=None)
ELIGIBLE={'INVALID_SCHEMA','DUPLICATE','INCOMPLETE_COVERAGE','UNRESOLVED_REFERENCE','IDENTITY_CONFLICT'}


def held(message):raise ContractError('SEMANTIC_OUTPUT_HELD','$',message)


def path_for(adapter,binding,phase,view):
    # Instructions, task/run IDs and retries cannot mint a new allowance.
    key=fingerprint({'node':binding['node'],'phase':phase,'input':view})
    return adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/output-attempts/{key}.json"


def write(path,record):_write_atomic(path,_json_bytes(record))


def interruption_status(adapter,workflow_id):
    """Read-only continuation inventory, never authority to replay or retry."""
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{workflow_id}/output-attempts'
    rows=[]
    for path in sorted(root.glob('*.json')):
        record=_load_json(path);attempts=record['attempts'];last=attempts[-1] if attempts else {}
        state=last.get('state','not_started')
        rows.append({'attempt_ref':path.relative_to(adapter.workflow.vault).as_posix(),
            'phase':record['phase'],'consumed_ordinals':[a['attempt'] for a in attempts],
            'state':state,'next_action':('revalidate_saved_response' if state=='valid' else
                'inspect_native_completion' if state=='reserved' else
                'use_remaining_correction' if state=='rejected' and last.get('correctable') and len(attempts)==1 else
                'held' if attempts else 'not_started'),
            'remaining_standard_calls':int(state=='rejected' and bool(last.get('correctable')) and len(attempts)==1)})
    return {'outputs':rows,'read_only':True,'retry_authorized':False}


def verify_stop_boundary(adapter,workflow_id,node,entry):
    """Bind an optional single-output limit to the existing cancel recovery."""
    import hashlib
    selected=entry.get('stop_after_output')
    if selected is None:return {}
    if not isinstance(selected,dict) or set(selected)!={'attempt_ref','attempt_sha256'}:
        held('stop_after_output requires the exact journal reference and SHA256')
    path=_vault_path(adapter.workflow.vault,selected['attempt_ref'])
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{workflow_id}/output-attempts'
    if path.parent!=root or hashlib.sha256(path.read_bytes()).hexdigest()!=selected['attempt_sha256']:
        held('interrupted output journal changed or belongs to another workflow')
    record=_load_json(path);attempts=record['attempts']
    if record['contract']!=CONTRACT or not attempts or len(attempts)>2:
        held('single-output continuation does not grant an exhausted allowance')
    for i,attempt in enumerate(attempts,1):
        if (attempt['attempt']!=i or attempt['attempt_id']!=fingerprint({'path':path.name,'attempt':i})
                or attempt['state'] not in ('valid','rejected') or not attempt.get('audit_ref')):
            held('uncertain native completion must be resolved before a bounded resume')
        audit_path=_vault_path(adapter.workflow.vault,attempt['audit_ref']);audit=_load_json(audit_path)
        if (audit_path.parent!=root.parent/'semantic-calls' or audit.get('workflow_id')!=workflow_id
                or audit.get('node')!=node or audit.get('phase')!=record['phase']
                or audit.get('native_task_id')!=attempt['task_id'] or audit.get('input_sha256')!=attempt['wire_sha256']
                or audit.get('schema_sha256')!=record['schema_sha256']):
            held('output boundary differs from its native audit identity')
    last=attempts[-1]
    if last['state']!='valid' and not (len(attempts)==1 and last.get('correctable')):
        held('no remaining standard correction; interruption cannot add an attempt')
    return {'output_boundary':{'journal_name':path.name,'phase':record['phase'],
        'input_sha256':record['input_sha256'],'schema_sha256':record['schema_sha256'],
        'preserved_attempt_count':len(attempts),'prior_attempts_sha256':fingerprint(attempts)}}


def output_boundary(adapter,binding):
    """The latest recovery of this node owns its execution limit."""
    import hashlib
    selected=None;pointer=None
    for p in adapter.workflow.status(binding['workflow_id']).get('execution_recovery_history',[]):
        rp=_vault_path(adapter.workflow.vault,p['report_ref'])
        if hashlib.sha256(rp.read_bytes()).hexdigest()!=p['report_sha256']:held('recovery evidence changed')
        report=_load_json(rp)
        for entry in report['entries']:
            if entry['node']==binding['node']:selected=entry.get('output_boundary');pointer=p
    return selected,pointer


def stop_at_output_boundary(adapter,binding,phase,view):
    """Stop after persisting the selected output and its caller's progress.

    A later explicit cancel recovery replaces this limit, without changing any
    attempt allowance, pause approval or semantic result.
    """
    selected,pointer=output_boundary(adapter,binding)
    if not selected:return
    path=path_for(adapter,binding,phase,view)
    if path.name!=selected['journal_name']:return
    record=_load_json(path);count=selected['preserved_attempt_count']
    if (record['input_sha256']!=selected['input_sha256'] or record['schema_sha256']!=selected['schema_sha256']
            or fingerprint(record['attempts'][:count])!=selected['prior_attempts_sha256']):
        held('bounded continuation changed the preserved attempts or input')
    if record['attempts'][-1]['state']!='valid':held('requested output boundary is not validated')
    ref=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/reports/output-boundary-{pointer['recovery_id']}.json"
    write(_vault_path(adapter.workflow.vault,ref),{'state':'reached','node':binding['node'],
        'task_id':binding['task_id'],'attempt_ref':path.relative_to(adapter.workflow.vault).as_posix(),
        'attempt_count':len(record['attempts']),'domain_submitted':False,'further_calls_authorized':False})
    raise ContractError('WORKFLOW_STOPPED','$','requested output boundary reached; progress saved; cancel and drain: '+ref)


def note_session(session):
    active=ACTIVE.get()
    if active:
        path,record,index=active;record['attempts'][index]['session_id']=session;write(path,record)


def note_audit(ref):
    active=ACTIVE.get()
    if active:
        path,record,index=active;record['attempts'][index]['audit_ref']=ref;write(path,record)


def attempt_id():
    active=ACTIVE.get()
    return fingerprint({'path':active[0].name,'attempt':active[2]+1}) if active else None


def check(phase,value,packet,state,draft):
    """Only this pure output-validation site admits mechanical corrections."""
    if phase=='resource-reduce':
        from fixed_reduce_packets import validate_packet
        validate_packet(packet,draft);return draft
    if phase in ('global-plan','global-detail'):
        from fixed_global_packets import accept_proposals,accept
        return accept_proposals(value,state,packet,draft) if phase=='global-plan' else (accept(value,state,packet,draft),[])
    from fixed_semantic_schema import validate
    validate(phase,draft)
    if 'blocked_reason' in draft:raise ContractError('SEMANTIC_REVIEW_REQUIRED','$',draft['blocked_reason'])
    return draft


def diagnosis(phase,packet,draft,error,*,state=None):
    result={'origin':'fixed-output-validation/v1','phase':phase,'code':error.code,'path':getattr(error,'path','$'),
        'rule':str(error),'instruction':'Return a complete legal answer for this same packet. Correct only the mechanical output contract; do not alter the evidence, broaden scope, or turn material uncertainty into approval.'}
    # Preserve the answer verbatim in native audit. Corrections return a full
    # replacement; the program never chooses or applies a domain patch.
    result['failed_response']=copy.deepcopy(draft)
    if error.code=='INVALID_SCHEMA':return result
    if packet.get('view',{}).get('output_contract')=='global-target-refs/v1':
        from fixed_global_targets import expand,offered
        result['allowed_target_refs']=[t['target_ref'] for t in offered(packet['view'])]
        try:draft=expand(phase,packet['view'],draft)
        except ContractError:return result
    if phase=='global-plan':
        allowed=packet['proposal_refs'];units={g['unit']:[p['proposal'] for p in g['proposals']] for g in packet['units']}
        assigned=[r for p in draft.get('pages',[]) for r in p.get('proposal_refs',[])] if isinstance(draft,dict) else []
        queries=draft.get('detail_unit_refs',[]) if isinstance(draft,dict) else []
        deferred={p for q in queries if isinstance(q,str) for p in units.get(q,[])}
        result.update(allowed_proposal_refs=allowed,allowed_detail_units=units,
            assigned_and_requested=sorted(set(assigned)&deferred),duplicate_assignments=[r for r,n in Counter(assigned).items() if n>1],
            missing=sorted(set(allowed)-set(assigned)-deferred),unknown=sorted(set(assigned)-set(allowed)))
        result['instruction']+=' For every proposal choose EITHER page assignment OR its detail unit, never both. The program does not choose which action you should keep.'
    elif phase in ('resource-reduce','global-detail'):
        result['allowed_candidate_refs']=packet['candidate_refs']
    if phase in ('global-plan','global-detail') and isinstance(draft,dict):
        view=packet.get('view',{});offered=view.get('coordination',{}).get('planned_pages',[])
        offered=[*offered,*view.get('existing_pages',[])]
        rows=draft.get('pages',[]);findings=[];targets={};path_groups={}
        key=lambda p:tuple(p.get('identity',{}).get(k) for k in ('kind','identity_key'))
        for i,page in enumerate(rows):
            refs=page.get('proposal_refs',page.get('candidate_refs',[]))
            path_groups.setdefault(page.get('path','').casefold(),[]).append(i)
            for j,target in enumerate(offered):
                if (page.get('path','').casefold()==target['path'].casefold() or key(page)==key(target)) and (page.get('identity')!=target['identity'] or page.get('path')!=target['path']):
                    ref='t'+str(j);targets[ref]={'identity':target['identity'],'path':target['path']}
                    findings.append({'rule':'append_requires_exact_offered_identity_and_path','output_index':i,
                        'affected_refs':refs,'offered_target_ref':ref})
        for indices in path_groups.values():
            if len({key(rows[i]) for i in indices})>1:
                findings.append({'rule':'distinct_identities_share_path','output_indices':indices,'path':rows[indices[0]]['path']})
        result['page_findings']=findings
        result['offered_targets']=targets
        if state is not None:
            # Acceptance checks the whole accumulated plan. Diagnose collisions
            # against that same state, without making an unoffered page selectable.
            offered_keys={key(p) for p in offered};reserved={}
            for j,target in enumerate(state['pages']):
                if key(target) in offered_keys:continue
                for i,page in enumerate(rows):
                    matches=[]
                    if key(page)==key(target):matches.append('identity')
                    if page.get('path','').casefold()==target['path'].casefold():matches.append('path')
                    if not matches:continue
                    ref='r'+str(j)
                    reserved[ref]={'identity':copy.deepcopy(target['identity']),'path':target['path'],'append_allowed':False}
                    differences=['identity.'+k for k in ('kind','identity_key','canonical_name','aliases')
                                 if page['identity'].get(k)!=target['identity'].get(k)]
                    if page['path']!=target['path']:differences.append('path')
                    findings.append({'rule':'occupied_planned_target_outside_offered_directory','output_index':i,
                        'affected_refs':page.get('proposal_refs',page.get('candidate_refs',[])),
                        'reserved_target_ref':ref,'colliding_fields':matches,'different_fields':differences})
            result['reserved_targets']=reserved
            result['instruction']+=' reserved_targets are occupied identity/path metadata only, NOT offered append targets. Do not append to them or assume semantic equivalence; choose a distinct valid identity/path or a permitted detail request. This diagnostic does not expand target authority.'
        result['instruction']+=' Resolve every listed finding. Output indices refer to failed_response.pages; offered_target_ref refers to offered_targets. Appending uses the complete offered identity (including aliases) and exact path; otherwise choose a distinct valid page or a permitted detail request. Decide every offered reference, including those without findings. Do not treat these diagnostics or the failed answer as evidence.'
        if view.get('output_contract')=='global-target-refs/v1':
            # The original answer remains in its authenticated audit. Send only
            # indexed conflicts; repeating every unaffected new identity can
            # overflow a correction although the actual conflict is tiny.
            affected=set(result.get('duplicate_assignments',[]))|set(result.get('assigned_and_requested',[]))|set(result.get('unknown',[]))
            indices={i for i,p in enumerate(result['failed_response'].get('pages',[]))
                     if affected.intersection(p.get('proposal_refs',p.get('candidate_refs',[])))}
            for finding in findings:
                indices.update(finding.get('output_indices',[]))
                if 'output_index' in finding:indices.add(finding['output_index'])
            original=result.pop('failed_response')
            result['failed_response_sha256']=fingerprint(original)
            result['failed_response_excerpt']={'pages':[{'output_index':i,'page':copy.deepcopy(original['pages'][i])} for i in sorted(indices)],
                'detail_unit_refs':copy.deepcopy(original.get('detail_unit_refs',[]))}
            result['instruction']+=' failed_response_excerpt contains indexed mechanical conflicts only; the complete unchanged response is retained in native audit. It is not the complete prior plan. Return a complete replacement deciding EVERY offered reference from the unchanged input, including unlisted rows; do not apply a partial patch or assume omitted rows were accepted.'
    return result


def continuation_permit(adapter,binding,path,record):
    """One explicit operator-authorized extra call, never a new automatic budget."""
    matches=[]
    for pointer in adapter.workflow.status(binding['workflow_id']).get('execution_recovery_history',[]):
        rp=_vault_path(adapter.workflow.vault,pointer['report_ref'])
        if __import__('hashlib').sha256(rp.read_bytes()).hexdigest()!=pointer['report_sha256']:held('recovery evidence changed')
        report=_load_json(rp)
        if report['request'].get('recovery_scope')!=SCOPE:continue
        for entry in report['entries']:
            permit=entry.get('exhausted_continuation')
            if entry['node']==binding['node'] and permit and permit['journal_name']==path.name:
                if (permit['prior_attempts_sha256']!=fingerprint(record['attempts'][:2])
                    or permit['input_sha256']!=record['input_sha256'] or permit['schema_sha256']!=record['schema_sha256']
                    or permit['attempt_ordinal']!=3):held('authorized continuation differs from its exact prior attempts')
                matches.append({**permit,'authorization_ref':pointer['report_ref']})
    if len(matches)>1:held('duplicate continuation authority')
    return matches[0] if matches else None


def read_saved_output(adapter,binding,phase,view,validator,prove,expected_ref):
    """Read-only full finite-journal proof inside a checked historical boundary."""
    from fixed_semantic import prepare_request_projection,decode_semantic_response
    path=path_for(adapter,binding,phase,view)
    if not path.is_file():held('historical success lacks its finite attempt journal')
    record=_load_json(path);sc=schema_for(phase,view);attempts=record.get('attempts',[])
    from reduce_disposition import output_permit, read_recovery
    disposition=output_permit(adapter,binding,path,record)
    if disposition and len(attempts)>disposition['preserved_attempt_count']:
        return read_recovery(adapter,binding,phase,view,validator,prove,expected_ref,path,record,disposition)
    if (record.get('contract')!=CONTRACT or record.get('phase')!=phase
        or record.get('input_sha256')!=fingerprint(view) or record.get('schema_sha256')!=fingerprint(sc)
        or not attempts):held('historical output identity differs')
    permit=continuation_permit(adapter,binding,path,record)
    if len(attempts)>(3 if permit else 2):held('historical extra attempt lacks its original authority')
    for index,attempt in enumerate(attempts):
        if (attempt.get('attempt')!=index+1 or attempt.get('attempt_id')!=fingerprint({'path':path.name,'attempt':index+1})
            or not attempt.get('audit_ref')):held('historical attempt identity or completion differs')
        correction=attempt.get('correction')
        if index==0 and correction is not None or index==2 and correction!=permit['diagnostic']:
            held('historical correction authority differs')
        prepared=prepare_request_projection(phase,view,sc,[],correction)
        if prepared['input_size']>30000 or fingerprint(prepared['wire'])!=attempt.get('wire_sha256'):
            held('historical full request or budget differs')
        ref=attempt['audit_ref'];ap=_vault_path(adapter.workflow.vault,ref);audit=_load_json(ap)
        if (audit.get('phase')!=phase or audit.get('native_task_id')!=attempt.get('task_id')
            or str(audit.get('native_run_id'))!=str(attempt.get('run_id'))
            or audit.get('output_attempt_id',attempt['attempt_id'])!=attempt['attempt_id']
            or attempt.get('session_id') and audit.get('model',{}).get('session_id')!=attempt['session_id']):
            held('historical journal differs from its native audit')
        found=prove(ap,prepared['wire'],sc,[])
        if found is None:held('historical output lacks an exact current native proof')
        try:
            try:draft,_=decode_semantic_response(found[0])
            except json.JSONDecodeError as exc:raise ContractError('INVALID_SCHEMA','$',str(exc)) from exc
            result=validator(draft)
        except ContractError as exc:
            if (attempt.get('state')!='rejected' or not attempt.get('correctable') or exc.code not in ELIGIBLE
                or index==len(attempts)-1):held('historical rejection/correction no longer matches a successful prefix')
            continue
        if index!=len(attempts)-1 or attempt.get('state')!='valid' or ref!=expected_ref:
            held('historical successful response/ordinal differs')
        return draft,ref,result
    held('historical prefix has no authenticated final success')


def execute(adapter,binding,phase,view,caller,model_call,validator,*,packet=None,state=None,attempt_limit=2):
    from fixed_semantic import prepare_request_projection,decode_semantic_response,HermesCaller,attach_receipt
    from fixed_semantic_media import image_catalog,select_images
    from fixed_semantic_recovery import native_reply
    adapter.worker_check(binding)
    path=path_for(adapter,binding,phase,view)
    output_schema=schema_for(phase,view)
    record=_load_json(path) if path.exists() else None
    if record is None:
        for pointer in adapter.workflow.status(binding['workflow_id']).get('execution_recovery_history',[]):
            rp=_vault_path(adapter.workflow.vault,pointer['report_ref'])
            if __import__('hashlib').sha256(rp.read_bytes()).hexdigest()!=pointer['report_sha256']:held('recovery evidence changed')
            report=_load_json(rp)
            if report['request'].get('recovery_scope')!=SCOPE:continue
            for entry in report['entries']:
                seed=entry.get('output_seed',{})
                if entry['node']==binding['node'] and seed.get('journal_name')==path.name:record=copy.deepcopy(seed['record'])
        record=record or {'contract':CONTRACT,'phase':phase,'input_sha256':fingerprint(view),'schema_sha256':fingerprint(output_schema),'attempts':[]}
    if record['schema_sha256']!=fingerprint(output_schema):held('schema changed; previous allowance is not reset')
    from reduce_disposition import output_permit, execute_output
    disposition=output_permit(adapter,binding,path,record)
    if disposition:
        return execute_output(adapter,binding,phase,view,caller,model_call,validator,path,record,disposition)
    images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence'])) if isinstance(caller,HermesCaller) and 'original_evidence' in view else []
    permit=continuation_permit(adapter,binding,path,record)
    if len(record['attempts'])>2 and not permit:held('extra attempt lacks explicit continuation authority')
    correction=None
    maximum=3 if permit else attempt_limit
    for index in range(maximum):
        if index==2:correction=copy.deepcopy(permit['diagnostic'])
        attempt=record['attempts'][index] if index<len(record['attempts']) else None
        prepared=prepare_request_projection(phase,view,output_schema,images,correction)
        if prepared['input_size']>30000:raise ContractError('READING_WINDOW_OVERSIZE','$','complete correction request exceeds bound; no truncation or call')
        reservation_history=[]
        if attempt:
            if attempt['wire_sha256']!=fingerprint(prepared['wire']):held('instructions or correction changed; attempt allowance is not reset')
            ref=attempt.get('audit_ref')
            if not ref:
                # A crash may occur between audit persistence and journal linking.
                root=path.parent.parent/'semantic-calls'
                matches=[p for p in root.glob('*.json') if _load_json(p).get('output_attempt_id')==attempt['attempt_id']]
                if len(matches)==1:ref=matches[0].relative_to(adapter.workflow.vault).as_posix();attempt['audit_ref']=ref;write(path,record)
            if not ref and not attempt.get('session_id'):
                for pointer in adapter.workflow.status(binding['workflow_id']).get('execution_recovery_history',[]):
                    rp=_vault_path(adapter.workflow.vault,pointer['report_ref'])
                    if __import__('hashlib').sha256(rp.read_bytes()).hexdigest()!=pointer['report_sha256']:held('recovery evidence changed')
                    report=_load_json(rp)
                    for entry in report['entries']:
                        permit=entry.get('uncalled_reservation',{})
                        if (report['request'].get('recovery_scope')==SCOPE and permit.get('journal_name')==path.name
                            and permit.get('attempt_id')==attempt['attempt_id'] and permit.get('task_id')==attempt['task_id']):
                            reservation_history=[*attempt.get('not_called_history',[]),{k:v for k,v in attempt.items() if k!='not_called_history'}]
                if reservation_history:attempt=None
            if attempt and not ref:held('call completion is uncertain; inspect native session '+str(attempt.get('session_id'))+' before any new request')
        if attempt:
            from hermes_source_units.runtime_telemetry import span
            with span('native_output_authentication',vault=adapter.workflow.vault,binding=binding,phase=phase):
                proof=native_reply(adapter,binding,_vault_path(adapter.workflow.vault,ref),prepared['wire'],output_schema,images,entry_checked=True)
            if proof is None:held('saved call lacks matching current native authority or exact request')
            raw=proof[0];call_ref=ref
            try:draft,_=decode_semantic_response(raw)
            except json.JSONDecodeError as exc:
                draft=None;error=ContractError('INVALID_SCHEMA','$',str(exc))
            else:error=None
        else:
            # Replay has fresh checks before/after native transcript proof.
            # New calls require fresh authority immediately before reservation.
            adapter.worker_check(binding)
            boundary,_=output_boundary(adapter,binding)
            if boundary and path.name!=boundary['journal_name']:
                raise ContractError('WORKFLOW_STOPPED','$','bounded resume permits saved replay and only its selected output call')
            if index and (not record['attempts'][0].get('correctable')):held('prior failure is not an output-contract error')
            attempt={'attempt':index+1,'attempt_id':fingerprint({'path':path.name,'attempt':index+1}),
                'task_id':binding['task_id'],'run_id':os.environ.get('HERMES_KANBAN_RUN_ID'),'wire_sha256':fingerprint(prepared['wire']),
                'state':'reserved','correction':copy.deepcopy(correction)}
            if index==2:attempt['authorization_ref']=permit['authorization_ref']
            if reservation_history:attempt['not_called_history']=reservation_history
            if index<len(record['attempts']):record['attempts'][index]=attempt
            else:record['attempts'].append(attempt)
            write(path,record)
            token=ACTIVE.set((path,record,index));call_ref=None;draft=None;error=None
            try:
                draft,call_ref=model_call(adapter,binding,phase,view,caller,30000,correction,attempt_limit=2 if correction else 1)
                note_audit(call_ref)
            except ContractError as exc:
                error=exc;call_ref=attempt.get('audit_ref') or getattr(exc,'semantic_audit_ref',None)
                if call_ref:note_audit(call_ref)
            finally:ACTIVE.reset(token)
        if error is None:
            try:result=validator(draft)
            except ContractError as exc:error=exc
        if error is None:
            attempt.update(state='valid',correctable=False);write(path,record)
            return draft,call_ref,result
        eligible=bool(call_ref and error.code in ELIGIBLE)
        if call_ref and attempt.get('state')!='rejected':attach_receipt(adapter,call_ref,{'ok':False,'validated':False,'state':'output_contract_rejected' if eligible else 'semantic_output_held','origin':'fixed-output-validation/v1','code':error.code,'reason':str(error)})
        # Historical diagnostics are part of the exact native correction wire.
        # Improving diagnostics cannot rewrite a consumed request or its budget.
        diagnostic=attempt.get('diagnostic') if attempt.get('state')=='rejected' else None
        attempt.update(state='rejected',correctable=eligible,diagnostic=diagnostic or (diagnosis(phase,packet or {},draft,error,state=state) if eligible else {'code':error.code,'reason':str(error)}))
        write(path,record)
        if call_ref:error.semantic_audit_ref=call_ref
        if not eligible:raise error
        if index==maximum-1:raise error
        # Freeze a correction only once reserved/sent. A pre-call size failure
        # consumes no second ordinal; a newer measured diagnostic may use that
        # same unused slot without rewriting the first call or its old diagnosis.
        correction=(record['attempts'][index+1]['correction'] if index+1<len(record['attempts'])
                    else diagnosis(phase,packet or {},draft,error,state=state))
    held('initial output and its one correction both failed; no further automatic call')
