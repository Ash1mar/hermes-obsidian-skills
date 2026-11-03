"""Program admission of native pages with explicit QA/source-completion gaps.

Uses the existing limited-scope kernel. No human approval or semantic answer is
manufactured: native wires/bodies are authenticated and task gaps stay open.
"""
import copy,hashlib,json
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.qa_policy import POLICY,enabled,reports as qa_reports
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint


def fail(message):raise ContractError('COVERAGE_SCOPE_INVALID','$',message)


def planning(adapter,binding,value,permitted):
    from fixed_global_replay import successful_prefix,recorded_packet
    from fixed_semantic import prepare_request_projection,decode_semantic_response
    from fixed_semantic_recovery import native_proof,_request_text
    from fixed_semantic_schema import schema_for
    from semantic_output import read_saved_output,check,path_for
    import fixed_global_packets as g
    from qa_program import apply_plan_exclusion
    refs=successful_prefix(adapter,binding,value);state=g.initial();remaining=g.units(value);pending=[];calls=[];used_gaps=[]
    prove=lambda p,w,s,i:native_proof(adapter,binding,p,w,s,i,permitted)
    candidates=[]
    for ref in qa_reports(adapter.workflow.vault,binding['workflow_id']):
        record=_load_json(_vault_path(adapter.workflow.vault,ref));scope=record['scope']
        if scope.get('source_input_sha256')!=fingerprint(value['view']) or 'planning_packet' not in scope:continue
        audit=_load_json(_vault_path(adapter.workflow.vault,record['evidence'][0]['audit_ref']))
        # The original finite journal owns the full correction wire and response.
        candidates.append((ref,record,audit))
    def consume_gaps():
        nonlocal state,remaining,pending
        while True:
            phase='global-detail' if pending else 'global-plan';found=None
            for ref,record,audit in candidates:
                if ref in used_gaps or audit['phase']!=phase:continue
                # Reconstruct exact packet framing, including a rejected packet.
                try:packet=recorded_packet(adapter,binding,value,remaining,state,record['evidence'][0]['audit_ref'],prepare_request_projection,pending=pending,allow_rejected=True)
                except ContractError:continue
                if packet['previous_state_sha256']!=g.state_digest(state):continue
                found=ref,record,packet;break
            if not found:return
            ref,record,packet=found
            if packet['candidate_refs']!=record['scope']['planning_packet'] or packet['proposal_refs']!=record['scope']['proposal_refs']:
                fail('QA planning exclusion changed its exact packet ownership')
            path=path_for(adapter,binding,phase,packet['view']);journal=_load_json(path);attempts=journal['attempts']
            if not 1<=len(attempts)<=2:fail('QA packet cannot gain another model allowance')
            last=None
            for index,a in enumerate(attempts,1):
                if a['attempt']!=index or a.get('state')!='rejected' or not a.get('audit_ref'):fail('QA planning attempt is unknown or changed')
                sc=schema_for(phase,packet['view']);wire=prepare_request_projection(phase,packet['view'],sc,[],a.get('correction'))['wire']
                if fingerprint(wire)!=a['wire_sha256']:fail('QA planning wire changed')
                result=prove(_vault_path(adapter.workflow.vault,a['audit_ref']),wire,sc,[])
                if result is None:fail('QA planning exclusion lacks native proof')
                try:
                    draft,_=decode_semantic_response(result[0]);check(phase,value,packet,state,draft)
                except ContractError as error:
                    if error.code not in ('SEMANTIC_REVIEW_REQUIRED','IDENTITY_CONFLICT','DUPLICATE','INVALID_SCHEMA','INCOMPLETE_COVERAGE','UNRESOLVED_REFERENCE'):raise
                    last=a['audit_ref']
                else:fail('valid native planning cannot be silently excluded')
            if last!=record['evidence'][0]['audit_ref']:fail('QA exclusion does not name its final consumed attempt')
            state=apply_plan_exclusion(state,packet,ref);used_gaps.append(ref)
            if pending:pending=pending[1:]
            else:remaining=remaining[packet['unit_count']:]
    for ref in refs:
        consume_gaps();phase='global-detail' if pending else 'global-plan'
        packet=recorded_packet(adapter,binding,value,remaining,state,ref,prepare_request_projection,pending=pending)
        _,call,(updated,requested)=read_saved_output(adapter,binding,phase,packet['view'],
            lambda d:check(phase,value,packet,state,d),prove,ref)
        if pending:pending=pending[1:]
        else:remaining=remaining[packet['unit_count']:];pending=requested
        state=updated;calls.append(call)
    consume_gaps()
    if remaining or pending:fail('unaccounted planning cannot be declared complete')
    return state,calls,prove,used_gaps


def manifest_for(adapter,w,binding,snapshot_ref,page_work_ref):
    from fixed_destinations import routes,compile_content
    value=_load_json(_vault_path(adapter.workflow.vault,snapshot_ref));work=_load_json(_vault_path(adapter.workflow.vault,page_work_ref))
    from fixed_semantic_recovery import permitted_tasks
    native=adapter.kanban.task_snapshot(w['kanban']['board_id'],binding['task_id'])
    permitted=permitted_tasks(adapter,binding);permitted[binding['task_id']]={'ended_run_ids':[str(r['id']) for r in native['runs']]}
    state,calls,prove,planning_gaps=planning(adapter,binding,value,permitted)
    from qa_program import isolate_pages
    # Existing native-body metadata is only a lookup hint; verify_pages checks
    # the exact finite journals, requests, responses and saved bytes below.
    written=[{**p,'content':_vault_path(adapter.workflow.vault,p['draft_ref']).read_text()} for p in work['written']]
    isolated=isolate_pages(adapter,binding,value,state,written,work['held'],None) if work['held'] else None
    effective=isolated[0] if isolated else state
    keep={p['path'] for p in effective['pages']};by_path={p['path']:p for p in written}
    gaps=[p for p in state['pages'] if p['path'] not in keep]
    destinations=routes(value,state['pages']);allowed=[]
    for page in effective['pages']:
        saved=by_path.get(page['path'])
        if saved is None:fail('included page has no saved native body')
        raw=_vault_path(adapter.workflow.vault,saved['draft_ref']).read_bytes();compiled,_=compile_content(value,{**page,'content':raw.decode()},destinations)
        allowed.append({**page,**{k:saved[k] for k in ('draft_ref','audit_ref','input_sha256')},
            'draft_sha256':hashlib.sha256(raw).hexdigest(),'storage_destination':destinations[page['path']],
            'submitted_content_sha256':hashlib.sha256(compiled.encode()).hexdigest(),
            'authored_content_sha256':hashlib.sha256((compiled.replace('\r\n','\n').replace('\r','\n').rstrip()+'\n').encode()).hexdigest()})
    omitted=list(effective['omitted_candidate_refs']);body_gap_refs={c for p in gaps for c in p['candidate_refs']}
    prior_omissions=[c for c in omitted if c not in body_gap_refs]
    return {'contract':'automatic-qa-coverage-manifest/v1','workflow_id':w['workflow_id'],
        'source_snapshot_ref':snapshot_ref,'source_snapshot_sha256':hashlib.sha256(_vault_path(adapter.workflow.vault,snapshot_ref).read_bytes()).hexdigest(),
        'native_page_work_ref':page_work_ref,'native_page_work_sha256':hashlib.sha256(_vault_path(adapter.workflow.vault,page_work_ref).read_bytes()).hexdigest(),
        'allowed_pages':allowed,'gaps':gaps,'omitted_planning_candidate_refs':prior_omissions,
        'allowed_page_count':len(allowed),'held_page_count':len(gaps),'deferred_candidate_occurrences':len(omitted),
        'planning_gap_refs':planning_gaps,'whole_batch_coverage_complete':False}


def verify(adapter,w,node,binding,entry,failure,runs):
    from reduce_coverage import verify_pages,journal_seals
    from hermes_source_units.reduce_coverage import task_partition
    if not enabled(w) or node.name!='global-reduce' or failure.get('error_code')!='REDUCTION_FAILED':fail('program QA scope requires its ended global submission')
    from fixed_reduce import preview_snapshot
    manifest=_load_json(_vault_path(adapter.workflow.vault,entry['manifest_ref']));approval=_load_json(_vault_path(adapter.workflow.vault,entry['approval_ref']))
    if (approval.get('contract')!='automatic-qa-scope-decision/v1' or approval.get('policy')!=POLICY
            or approval.get('workflow_id')!=w['workflow_id'] or approval.get('actor')!=w['actor']
            or approval.get('accept_necessary_coverage_gaps') is not True
            or approval.get('stop_after')!='workflow_configured_boundary'
            or approval.get('human_review_performed') is not False or approval.get('new_model_calls_granted')!=0
            or approval.get('manifest_sha256')!=entry['manifest_sha256']):fail('scope has no exact program QA policy authority')
    for field in ('manifest','approval'):
        if not entry[field+'_ref'].startswith(f'_system/ledgers/ingest-workflows/{w["workflow_id"]}/coverage-scopes/'):
            fail('program scope must belong to this workflow')
        if hashlib.sha256(_vault_path(adapter.workflow.vault,entry[field+'_ref']).read_bytes()).hexdigest()!=entry[field+'_sha256']:fail('program scope evidence changed')
    source=_load_json(_vault_path(adapter.workflow.vault,manifest['source_snapshot_ref']));current=preview_snapshot(adapter,binding)
    if any(source[k]!=current[k] for k in ('contract','view','backend')):fail('program QA scope changed source authority')
    rebuilt=manifest_for(adapter,w,binding,manifest['source_snapshot_ref'],manifest['native_page_work_ref'])
    if rebuilt!=manifest:fail('program scope is not the exact native/QA partition')
    from fixed_semantic_recovery import permitted_tasks
    permitted=permitted_tasks(adapter,binding);permitted[binding['task_id']]={'ended_run_ids':[str(r['id']) for r in runs]}
    state,calls,prove,gaps=planning(adapter,binding,current,permitted)
    written=verify_pages(adapter,binding,current,manifest,prove)
    rows=[{'path':p['storage_destination'],'identity':p['identity'],
        'candidate_refs':[r for c in p['candidate_refs'] for r in current['backend']['mapping'][c]],
        'submitted_content_sha256':p['submitted_content_sha256'],'authored_content_sha256':p['authored_content_sha256']} for p in manifest['allowed_pages']]
    omitted=[c for p in manifest['gaps'] for c in p['candidate_refs']]+manifest['omitted_planning_candidate_refs']
    if set(c for p in manifest['allowed_pages'] for c in p['candidate_refs'])|set(omitted)!=set(current['backend']['mapping']):fail('QA scope lost candidate accounting')
    proof={k:entry[k] for k in ('manifest_ref','manifest_sha256','approval_ref','approval_sha256')}
    proof.update(pages=rows,omitted_candidate_refs=[r for c in omitted for r in current['backend']['mapping'][c]],
        new_model_calls_granted=0,native_bodies_authenticated=len(written),native_planning_calls_authenticated=len(calls),
        gaps_completed=False,source_snapshot_sha256=manifest['source_snapshot_sha256'],program_policy_admission=POLICY)
    proof.update(task_partition(adapter.workflow.knowledge,current['backend']['tasks'],rows));proof['finite_journal_seals']=journal_seals(adapter,manifest)
    return {'coverage_admission':proof,'original_calls_preserved':True}
