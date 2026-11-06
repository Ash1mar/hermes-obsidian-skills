"""Automatic quality isolation. No operator adjudicates source or model claims."""
import copy,hashlib,json,sqlite3
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.qa_policy import POLICY,enabled,gap
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint

SCOPE='qa-policy/v1'


def execution_code_fingerprint():
    root=Path(__file__).parent
    return fingerprint({n:hashlib.sha256((root/n).read_bytes()).hexdigest()
        for n in ('page_protocol.py','page_pair_eligibility.py','page_budget_store.py','split_page_calls.py','page_request.py',
                  'qa_program.py','fixed_page_work.py','fixed_semantic.py',
                  'native_page_pairs.py','page_pair_calls.py','page_dependencies.py','durable_page_replay.py','page_budget_hold.py',
                  'source_unknown_recovery.py','native_stream_policy.py','returned_native_output.py',
                  'network_connection_retry.py')})


def isolate_plan(adapter,binding,value,state,packet,error):
    if (not enabled(adapter.workflow.status(binding['workflow_id']))
            or error.code not in ('SEMANTIC_REVIEW_REQUIRED','IDENTITY_CONFLICT','DUPLICATE','INVALID_SCHEMA','INCOMPLETE_COVERAGE','UNRESOLVED_REFERENCE')
            or not getattr(error,'semantic_audit_ref',None)):return None
    used={c for p in state['pages'] for c in p['candidate_refs']}|set(state['omitted_candidate_refs'])
    if used.intersection(packet['candidate_refs']):raise ContractError('INCOMPLETE_COVERAGE','$','QA packet overlaps previously assigned evidence')
    ref=gap(adapter.workflow.vault,binding['workflow_id'],binding['node'],
        {'planning_packet':packet['candidate_refs'],'proposal_refs':packet['proposal_refs'],
         'source_input_sha256':fingerprint(value['view'])},
        [{'code':error.code,'reason':str(error),'audit_ref':error.semantic_audit_ref}])
    return apply_plan_exclusion(state,packet,ref)


def apply_plan_exclusion(state,packet,ref):
    result=copy.deepcopy(state)
    result['omitted_candidate_refs'].extend(packet['candidate_refs'])
    result['proposal_refs'].extend(packet['proposal_refs'])
    result['reasons'].append('Program isolated an unresolved QA planning packet; '+ref)
    return result


def isolate_pages(adapter,binding,value,state,written,held,planning_failure):
    if not enabled(adapter.workflow.status(binding['workflow_id'])):return None
    if planning_failure:return None  # Planning owns its own packet isolation.
    from page_budget_hold import verified
    by_path={p['path']:p for p in state['pages']}
    for h in held:
        if h.get('origin')=='required-page-evidence/v1' and h.get('path') in by_path:
            from fixed_reduce import page_view
            from page_pair_eligibility import inspect
            observed=page_view(adapter,value,by_path[h['path']]);observed['page_scope_contract']='assigned-candidates/v1'
            if inspect(observed)==h.get('evidence_proof') and h['evidence_proof']['required_missing']:continue
            return None
        if h.get('code')=='SEMANTIC_REVIEW_REQUIRED' and h.get('audit_ref'):continue
        if h.get('path') in by_path and verified(adapter,value,by_path[h['path']],h):continue
        return None
    from hermes_source_units.delivery_scope import dependency_partition
    bodies={p['path']:p for p in written};seed={h['path']:{'category':'qa_unresolved','reason':h['reason']} for h in held}
    pages=[{'page_id':p['path'],'path':p['path'],'identity':p['identity'],
        'content':bodies.get(p['path'],{}).get('content','')} for p in state['pages']]
    partition=dependency_partition(pages,seed)
    # QA closure follows actual links to the affected pages. Other unresolved
    # links retain their separate normal validation/implementation contract.
    excluded=set(seed);changed=True
    while changed:
        before=set(excluded)
        excluded.update(pid for pid,deps in partition['dependencies'].items() if excluded.intersection(deps))
        changed=excluded!=before
    kept=[p for p in state['pages'] if p['path'] not in excluded]
    if not kept:raise ContractError('NO_DELIVERABLE_PAGES','$','all pages isolated; zero-page acceptance required, no publication')
    removed=[p for p in state['pages'] if p['path'] in excluded]
    ref=gap(adapter.workflow.vault,binding['workflow_id'],binding['node'],
        {'pages':removed,'candidate_refs':[c for p in removed for c in p['candidate_refs']]},held,
        dependents=sorted(excluded-set(seed)))
    result=copy.deepcopy(state);result['pages']=kept
    result['omitted_candidate_refs'].extend(c for p in removed for c in p['candidate_refs'])
    result['reasons'].append('Program QA isolation; no unsupported page accepted; '+ref)
    return result,ref


def page_link_failures(value,state,written):
    """Classify native authored-link failures without editing prose or guessing assets."""
    from fixed_destinations import routes,compile_content
    destinations=routes(value,state['pages']);held=[]
    for p in written:
        try:compile_content(value,p,destinations)
        except ContractError as error:
            if error.code!='INVALID_OUTPUT_PATH':raise
            held.append({'path':p['path'],'candidate_refs':p['candidate_refs'],'phase':'page-write',
                'code':'SEMANTIC_REVIEW_REQUIRED','reason':str(error),'audit_ref':p['audit_ref'],
                'origin':'native-page-destination-check/v1'})
    return held


from page_budget_store import operation as page_read_operation

@page_read_operation()
def verify_recovery(adapter,w,node,binding,entry,failure,runs):
    """Authenticate current immutable inputs and ended known native responses."""
    from fixed_reduce import preview_snapshot
    from fixed_semantic import system_prompt
    from fixed_semantic_recovery import _request_text
    if not enabled(w) or node.name!='global-reduce' or failure.get('error_code') not in ('SEMANTIC_REVIEW_REQUIRED','INVALID_OUTPUT_PATH'):
        raise ContractError('RECOVERY_NOT_ALLOWED','$','only a known global QA hold may use automatic continuation')
    path=_vault_path(adapter.workflow.vault,entry['snapshot_ref']);old=_load_json(path)
    if (hashlib.sha256(path.read_bytes()).hexdigest()!=entry['snapshot_sha256'] or old.get('binding')!=binding
            or fingerprint({k:v for k,v in old.items() if k!='input_id'})!=old['input_id']):
        raise ContractError('STALE_INPUT','$','original QA snapshot changed')
    current=preview_snapshot(adapter,binding)
    if any(old[k]!=current[k] for k in ('contract','view','backend')):raise ContractError('STALE_INPUT','$','QA continuation cannot change materials')
    code=execution_code_fingerprint()
    if entry.get('execution_code_fingerprint')!=code:raise ContractError('STALE_INPUT','$','QA recovery executor changed')
    root=path.parent.parent;calls=[];allowed={binding['task_id']:{str(r['id']) for r in runs}}
    for pointer in w.get('execution_recovery_history',[]):
        report_path=_vault_path(adapter.workflow.vault,pointer['report_ref']);raw=report_path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=pointer['report_sha256']:raise ContractError('STALE_INPUT','$','QA ancestor authority changed')
        report=json.loads(raw)
        if report['request'].get('recovery_scope')!=SCOPE:continue
        for prior in report['entries']:
            if prior['node']==node.name:
                allowed[prior['previous_task_id']]=set(prior['ended_run_ids'])
                if prior.get('qa_policy',{}).get('execution_code_fingerprint')==code:
                    raise ContractError('RECOVERY_EXHAUSTED','$','unchanged program cannot repeat automatic QA continuation')
    with sqlite3.connect('file:'+str(Path.home()/'.hermes/state.db')+'?mode=ro',uri=True) as db:
        for p in (root/'semantic-calls').glob('*.json'):
            call=_load_json(p)
            if call.get('native_task_id') not in allowed or call.get('reused_from_audit'):continue
            phase=call['phase'];sid=call['model']['session_id']
            if phase not in ('global-plan','global-detail','page-write','page-pair') or str(call.get('native_run_id')) not in allowed[call['native_task_id']]:
                raise ContractError('RECOVERY_NOT_ALLOWED','$','unrecognized QA native call')
            if phase=='page-pair':
                from durable_page_replay import authenticate_request
                from fixed_semantic_recovery import native_proof
                permitted={task:{'ended_run_ids':list(ids)} for task,ids in allowed.items()}
                prove=lambda p,wire,sc,images:native_proof(adapter,binding,p,wire,sc,images,permitted)
                ledger,q,view=authenticate_request(adapter,binding,old,call['page_request_id'],prove)
                if q['response']['audit_ref']!=p.relative_to(adapter.workflow.vault).as_posix():
                    raise ContractError('RECOVERY_NOT_ALLOWED','$','page native audit differs')
                calls.append({'ref':p.relative_to(adapter.workflow.vault).as_posix(),
                    'semantic_sha256':fingerprint({k:v for k,v in call.items() if k not in ('receipt_refs','validated')})})
                continue
            rows=list(db.execute('select role,content,tool_calls from messages where session_id=?',(sid,)))
            users=[c for r,c,_ in rows if r=='user' and fingerprint(c)==call['user_sha256']]
            body=json.loads(_request_text(users[0])) if len(users)==1 else {}
            if (db.execute('select source from sessions where id=?',(sid,)).fetchone()!=('kanban',)
                    or len(users)!=1 or any(tc and tc!='[]' for _,_,tc in rows)
                    or not any(r=='assistant' and fingerprint(c)==call['response_sha256'] for r,c,_ in rows)
                    or body.get('phase')!=phase
                    or fingerprint({'system':system_prompt(phase,body['input'],body.get('correction')),'user':users[0]})!=call['input_sha256']):
                raise ContractError('RECOVERY_NOT_ALLOWED','$','QA native request/response proof differs')
            calls.append({'ref':p.relative_to(adapter.workflow.vault).as_posix(),
                'semantic_sha256':fingerprint({k:v for k,v in call.items() if k not in ('receipt_refs','validated')})})
    if not calls:raise ContractError('RECOVERY_NOT_ALLOWED','$','no ended returned native QA evidence')
    for p in (root/'output-attempts').glob('*.json'):
        for a in _load_json(p).get('attempts',[]):
            if a.get('task_id')==binding['task_id'] and a.get('state')=='reserved' and not a.get('audit_ref'):
                raise ContractError('RECOVERY_NOT_ALLOWED','$','unknown completion is not a QA failure')
    link_proof={}
    if failure.get('error_code')=='INVALID_OUTPUT_PATH':
        from fixed_page_work import measure
        from fixed_semantic import HermesCaller,prepare_request_projection
        from fixed_page_replay import read_outcome
        from fixed_semantic_recovery import native_proof,permitted_tasks
        from fixed_semantic_media import select_images,image_catalog
        wp=root/f'semantic-inputs/page-work-{old["input_id"]}.json';work=_load_json(wp)
        if work['binding']!=binding or work.get('planning_failure') or work.get('formal_global_submitted'):
            raise ContractError('RECOVERY_NOT_ALLOWED','$','link recovery needs exact unsubmitted native page work')
        written=[{**p,'content':_vault_path(adapter.workflow.vault,p['draft_ref']).read_text()} for p in work['written']]
        bad=page_link_failures(old,{'pages':written},written)
        if not bad:raise ContractError('RECOVERY_NOT_ALLOWED','$','no reproducible authored-link failure')
        permitted=permitted_tasks(adapter,binding);permitted[binding['task_id']]={'ended_run_ids':[str(r['id']) for r in runs]}
        prove=lambda p,wire,sc,images:native_proof(adapter,binding,p,wire,sc,images,permitted)
        for bad_page in bad:
            row=next(p for p in written if p['path']==bad_page['path'])
            page={k:row[k] for k in ('identity','path','candidate_refs')};view,size=measure(adapter,old,page,HermesCaller(),prepare_request_projection)
            images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence']))
            result=read_outcome(adapter,binding,view,prove,images)
            if not result or result['kind']!='valid' or result['ref']!=row['audit_ref'] or result['draft']['content']!=row['content']:
                raise ContractError('RECOVERY_NOT_ALLOWED','$','invalid link body lacks exact native finite proof')
        link_proof={'page_link_failures':bad,'native_page_work_ref':wp.relative_to(adapter.workflow.vault).as_posix(),
            'native_page_work_sha256':hashlib.sha256(wp.read_bytes()).hexdigest()}
    return {'qa_policy':{'contract':SCOPE,'policy':POLICY,'snapshot_ref':entry['snapshot_ref'],
        'execution_code_fingerprint':code,
        'snapshot_sha256':entry['snapshot_sha256'],'native_calls':calls,'domain_inputs_preserved':True,
        'original_attempts_preserved':True,'new_call_budget_granted':0,'unknown_completion_allowed':False,**link_proof}}


def recover_global(adapter,wid,*,resume=True):
    from automatic_pass_recovery import request
    from native_ingest import control_workflow
    from native_stop_inventory import verify_cancel_drain
    from hermes_source_units.source_units import _exclusive_lock
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}'
    with _exclusive_lock(root/'automatic-qa/.controller.lock'):
        w=adapter.workflow.status(wid)
        if not enabled(w) or w['current_stage']!='reducing' or adapter.workflow.pending_pause(w):return False
        card=next((c for c in w['kanban']['task_map'] if c['node']=='global-reduce'),None)
        fp=root/f'reports/failed-{fingerprint("global-reduce")[:16]}.json'
        if not card or not fp.exists():return False
        failure=_load_json(fp)
        if failure.get('task_id')!=card['task_id'] or failure.get('error_code') not in ('SEMANTIC_REVIEW_REQUIRED','REDUCTION_FAILED','INVALID_OUTPUT_PATH'):return False
        coverage=failure.get('error_code')=='REDUCTION_FAILED'
        candidates=[p for p in (root/'semantic-inputs').glob('*.json')
            if (v:=_load_json(p)).get('contract')=='fixed-reduce-input/v1' and v.get('binding',{}).get('task_id')==card['task_id']]
        if len(candidates)!=1:raise ContractError('RECOVERY_NOT_ALLOWED','$','QA recovery needs one exact source snapshot')
        snapshot=candidates[0];code=execution_code_fingerprint();op='auto-qa-'+fingerprint({'node':card['node'],'task':card['task_id'],'code':code})[:24]
        if not w['cancel_requested']:control_workflow(adapter.workflow.vault,wid,'cancel',op+'-stop')
        verify_cancel_drain(adapter,wid,timeout=0)
        if coverage:
            return recover_coverage(adapter,wid,card,snapshot,fp,op,resume=resume)
        rp=root/f'automatic-qa/{op}.request.json'
        if not rp.exists():_write_atomic(rp,_json_bytes(request(adapter,wid,recovery_id=op,recovery_scope=SCOPE,
            reason='Automatic all-stage QA policy; reuse native bodies, isolate blocked work and actual dependents; no human review',
            entries=[{'node':card['node'],'task_id':card['task_id'],'execution_code_fingerprint':code,'failure_sha256':hashlib.sha256(fp.read_bytes()).hexdigest(),
                'snapshot_ref':snapshot.relative_to(adapter.workflow.vault).as_posix(),'snapshot_sha256':hashlib.sha256(snapshot.read_bytes()).hexdigest()}])))
        result=adapter.recover_reduce_execution(_load_json(rp))
        _write_atomic(root/f'automatic-qa/{op}.json',_json_bytes(result))
        if resume:control_workflow(adapter.workflow.vault,wid,'resume',op+'-resume')
        return True


def recover_coverage(adapter,wid,card,snapshot,failure_path,op,*,resume=True):
    from automatic_pass_recovery import request
    from native_ingest import control_workflow
    from qa_scope import manifest_for
    from reduce_coverage import contract_hash
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}'
    w=adapter.workflow.status(wid);batch=adapter.workflow.knowledge._batch(w['batch_id'])
    allowed='context or partial reading cannot complete the target'
    if not batch.get('failures') or any(f.get('stage')!='reduce' or f.get('code')!='INCOMPLETE_COVERAGE' or allowed not in f.get('message','') for f in batch['failures']):
        raise ContractError('RECOVERY_NOT_ALLOWED','$','global submission failure is not the known incomplete-work contract')
    binding=_load_json(snapshot)['binding'];work=root/f'semantic-inputs/page-work-{_load_json(snapshot)["input_id"]}.json'
    if not work.exists():raise ContractError('RECOVERY_NOT_ALLOWED','$','missing native page work')
    request_path=root/f'automatic-qa/{op}-coverage.request.json'
    if not request_path.exists():
        manifest=manifest_for(adapter,w,binding,snapshot.relative_to(adapter.workflow.vault).as_posix(),work.relative_to(adapter.workflow.vault).as_posix())
        folder=root/'coverage-scopes';mp=folder/('program-qa-'+fingerprint(manifest)+'.json');_write_atomic(mp,_json_bytes(manifest));mh=hashlib.sha256(mp.read_bytes()).hexdigest()
        approval={'contract':'automatic-qa-scope-decision/v1','workflow_id':wid,'actor':w['actor'],'policy':POLICY,
            'human_review_performed':False,'accept_necessary_coverage_gaps':True,'manifest_sha256':mh,
            'stop_after':'workflow_configured_boundary','new_model_calls_granted':0}
        ap=folder/('program-qa-decision-'+fingerprint(approval)+'.json');_write_atomic(ap,_json_bytes(approval))
        q=request(adapter,wid,recovery_id=op+'-scope',recovery_scope='reduce-coverage/v1',target_contract_sha256=contract_hash(),
            reason='Automatic QA policy scope; native evidence only, incomplete source work remains incomplete, no human review',
            entries=[{'node':'global-reduce','task_id':card['task_id'],'automatic_qa':True,
                'failure_sha256':hashlib.sha256(failure_path.read_bytes()).hexdigest(),
                'manifest_ref':mp.relative_to(adapter.workflow.vault).as_posix(),'manifest_sha256':mh,
                'approval_ref':ap.relative_to(adapter.workflow.vault).as_posix(),'approval_sha256':hashlib.sha256(ap.read_bytes()).hexdigest()}])
        _write_atomic(request_path,_json_bytes(q))
    result=adapter.recover_reduce_execution(_load_json(request_path));_write_atomic(root/f'automatic-qa/{op}-coverage.json',_json_bytes(result))
    if resume:control_workflow(adapter.workflow.vault,wid,'resume',op+'-coverage-resume')
    return True
