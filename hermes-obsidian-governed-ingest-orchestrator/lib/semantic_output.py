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
from fixed_semantic_schema import schema

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


def diagnosis(phase,packet,draft,error):
    result={'origin':'fixed-output-validation/v1','phase':phase,'code':error.code,'path':getattr(error,'path','$'),
        'rule':str(error),'instruction':'Return a complete legal answer for this same packet. Correct only the mechanical output contract; do not alter the evidence, broaden scope, or turn material uncertainty into approval.'}
    # Preserve the failed answer verbatim as data. The model must return a full
    # replacement; the program never chooses or applies a domain patch.
    result['failed_response']=copy.deepcopy(draft)
    if error.code=='INVALID_SCHEMA':return result
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
        result['instruction']+=' Resolve every listed finding. Output indices refer to failed_response.pages; offered_target_ref refers to offered_targets. Appending uses the complete offered identity (including aliases) and exact path; otherwise choose a distinct valid page or a permitted detail request. Decide every offered reference, including those without findings. Do not treat these diagnostics or the failed answer as evidence.'
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


def execute(adapter,binding,phase,view,caller,model_call,validator,*,packet=None):
    from fixed_semantic import prepare_request_projection,decode_semantic_response,HermesCaller,attach_receipt
    from fixed_semantic_media import image_catalog,select_images
    from fixed_semantic_recovery import native_reply
    adapter.worker_check(binding)
    path=path_for(adapter,binding,phase,view)
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
        record=record or {'contract':CONTRACT,'phase':phase,'input_sha256':fingerprint(view),'schema_sha256':fingerprint(schema(phase)),'attempts':[]}
    if record['schema_sha256']!=fingerprint(schema(phase)):held('schema changed; previous allowance is not reset')
    images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence'])) if isinstance(caller,HermesCaller) and 'original_evidence' in view else []
    permit=continuation_permit(adapter,binding,path,record)
    if len(record['attempts'])>2 and not permit:held('extra attempt lacks explicit continuation authority')
    correction=None
    for index in range(3 if permit else 2):
        adapter.worker_check(binding)
        if index==2:correction=copy.deepcopy(permit['diagnostic'])
        attempt=record['attempts'][index] if index<len(record['attempts']) else None
        prepared=prepare_request_projection(phase,view,schema(phase),images,correction)
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
            proof=native_reply(adapter,binding,_vault_path(adapter.workflow.vault,ref),prepared['wire'],schema(phase),images)
            if proof is None:held('saved call lacks matching current native authority or exact request')
            raw=proof[0];call_ref=ref
            try:draft,_=decode_semantic_response(raw)
            except json.JSONDecodeError as exc:
                draft=None;error=ContractError('INVALID_SCHEMA','$',str(exc))
            else:error=None
        else:
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
        attempt.update(state='rejected',correctable=eligible,diagnostic=diagnostic or (diagnosis(phase,packet or {},draft,error) if eligible else {'code':error.code,'reason':str(error)}))
        write(path,record)
        if call_ref:error.semantic_audit_ref=call_ref
        if not eligible:raise error
        if index==(2 if permit else 1):raise error
        # Freeze a correction only once reserved/sent. A pre-call size failure
        # consumes no second ordinal; a newer measured diagnostic may use that
        # same unused slot without rewriting the first call or its old diagnosis.
        correction=(record['attempts'][index+1]['correction'] if index+1<len(record['attempts'])
                    else diagnosis(phase,packet or {},draft,error))
    held('initial output and its one correction both failed; no further automatic call')
