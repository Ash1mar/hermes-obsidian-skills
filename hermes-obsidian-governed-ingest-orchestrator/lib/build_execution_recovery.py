"""Stopped Build admission and exact review reuse; no implicit semantic retries."""
import copy,hashlib,json
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path,_json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint

SCOPE='build-native/v1'
GRAPH_INTERRUPTION='SOURCE_CHANGED at $: graph dependencies changed during calculation'

def fail(message):raise ContractError('RECOVERY_NOT_ALLOWED','$',message)


def graph_interruption(adapter,binding,failure,runs):
    """Authenticate a native pre-execution cancellation, never a content retry.

    The native runner's scheduled cancellation outcome is positive evidence;
    missing audit/session data alone is not evidence of an uncalled request.
    """
    if failure.get('error_code')!='SOURCE_CHANGED' or failure.get('reason')!=GRAPH_INTERRUPTION:
        fail('only the exact graph-calculation interruption can use this admission')
    workflow=adapter.workflow.status(binding['workflow_id'])
    native=adapter.kanban.task_snapshot(workflow['kanban']['board_id'],binding['task_id'])
    task=native.get('task',{})
    if (task.get('id')!=binding['task_id'] or task.get('status')!='archived'
            or task.get('created_by')!='ingest-workflow' or task.get('worker_pid')
            or task.get('current_run_id') or task.get('session_id') or native.get('runs')!=runs
            or not runs or any(r.get('profile')!='ingest-program' or r.get('status')!='scheduled'
                or r.get('outcome')!='scheduled' or r.get('summary')!='Vault workflow cancelled'
                or not r.get('ended_at') for r in runs)):
        fail('graph interruption lacks a trusted ended pre-execution cancellation outcome')
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}"
    for folder in ('semantic-calls','call-failures','output-attempts'):
        for path in (root/folder).glob('*.json'):
            record=_load_json(path)
            if (record.get('native_task_id',record.get('task_id'))==binding['task_id']
                    or any(a.get('task_id')==binding['task_id'] for a in record.get('attempts',[]))):
                fail('graph interruption already has a consumed or uncertain domain attempt')
    return {'contract':'build-preexecution-graph-interruption/v1',
        'ended_run_ids':[str(r['id']) for r in runs], 'native_outcome':'scheduled_cancellation',
        'new_domain_calls_granted':0,'original_review_history_preserved':True}

def storage_interruption(adapter,binding,failure,entry):
    """Recover only a positively recorded mechanical-container seal conflict."""
    if (failure.get('error_code')!='EVIDENCE_SNAPSHOT_REVOKED'
            or failure.get('reason')!='EVIDENCE_SNAPSHOT_REVOKED at $: protected write requested revocation; stop affected consumers before controlled retry'
            or entry.get('retry_unknowns')):
        fail('only the exact protected-output-container interruption can recover without new calls')
    from hermes_source_units.evidence_snapshot import ROOT,flags
    from hermes_source_units.partial_finalize import pinned,pin
    from hermes_source_units.finalize_revision_state import load,work_id
    vault=adapter.workflow.vault;w=adapter.workflow.status(binding['workflow_id'])
    state_ref=f"{ROOT}/{binding['workflow_id']}/state.json"
    state=_load_json(vault/state_ref);manifest_ref=f"{ROOT}/{binding['workflow_id']}/manifest.json"
    manifest=pinned(vault,{'ref':manifest_ref,'sha256':state['manifest_sha256']})
    if (state.get('state')!='revoked' or state.get('workflow_id')!=binding['workflow_id']
            or state.get('generation')!=manifest.get('generation')):fail('old protector interruption evidence differs')
    prefix='controlled write requested: ';reason=state.get('reason','')
    if not reason.startswith(prefix):fail('revocation was not caused by a mechanical output-container write')
    path=Path(reason[len(prefix):]);allowed=[vault/'_system/knowledge-builds/.finalize.lock',vault/'_system/knowledge-builds/page-revisions']
    if path not in allowed:fail('revocation affected source/evidence, not an authorized mechanical output container')
    for name,previous in manifest['original_flags'].items():
        if flags(Path(name))!=previous:fail('old snapshot flags have not been restored')
    controls=load(vault,binding['workflow_id'])
    snapshot=_load_json(_vault_path(vault,entry['snapshot_ref']))
    for run in snapshot['backend']['runs']:
        permission=controls['works'].get(work_id(run['run_id'],'*'),{})
        if not {p.relative_to(vault).as_posix() for p in allowed}.issubset(permission.get('storage_prepared',[])):
            fail('mechanical output layout has not been prepared by the stopped operator')
    if allowed[0].exists() or not allowed[1].is_dir():fail('marker-free mechanical output layout is not present')
    # The runtime registry is replaced by the next protector generation. Keep
    # its actual data inside the immutable recovery report, not a stale pin to
    # that mutable registry path.
    return {'contract':'build-output-storage-interruption/v1','protector_state':state,
        'protector_manifest':manifest,'verified_manifest_sha256':state['manifest_sha256'],'control_head':controls['head'],
        'corrected_paths':[p.relative_to(vault).as_posix() for p in allowed],
        'new_domain_calls_granted':0,'source_protection_unchanged':True}

def empty_lock_interruption(adapter,binding,failure,entry):
    """Only the recorded empty storage-preparation artifact is recoverable."""
    from hermes_source_units.partial_finalize import pinned
    from hermes_source_units.finalize_revision_state import load,work_id
    vault=adapter.workflow.vault;ref='_system/knowledge-builds/.finalize.lock'
    if (failure.get('error_code')!='LOCK_RECOVERY_REQUIRED' or entry.get('retry_unknowns')
            or failure.get('reason')!=f'LOCK_RECOVERY_REQUIRED at $: Legacy or foreign-host lock requires supported recovery: {vault/ref}'):
        fail('lock recovery does not match the exact empty preparation artifact')
    controls=load(vault,binding['workflow_id'])
    snapshot=pinned(vault,{'ref':entry['snapshot_ref'],'sha256':entry['snapshot_sha256']})
    proofs=[]
    for run in snapshot['backend']['runs']:
        permission=controls['works'].get(work_id(run['run_id'],'*'),{})
        proof=permission.get('repaired_empty_lock',{})
        if (permission.get('state')!='enabled' or proof.get('ref')!=ref
                or proof.get('sha256')!=hashlib.sha256(b'').hexdigest() or not proof.get('previous_control_head')):
            fail('empty artifact repair lacks its original authorized preparation proof')
        proofs.append(proof)
    if (vault/ref).exists() or not (vault/'_system/knowledge-builds/page-revisions').is_dir():
        fail('empty marker was not repaired or output storage is missing')
    return {'contract':'build-empty-lock-interruption/v1','repairs':proofs,'control_head':controls['head'],
        'new_domain_calls_granted':0,'source_protection_unchanged':True}


def contract_hash():
    root=Path(__file__).parent
    return hashlib.sha256(b''.join((root/n).read_bytes() for n in (
        'build_execution_recovery.py','reduce_execution_recovery.py','fixed_review.py',
        'semantic_output.py','fixed_semantic.py','native_call_state.py','build_disposition.py',
        'build_evidence.py','finalize_revision.py','finalize_disposition_control.py',
        'hermes_source_units/partial_finalize.py','hermes_source_units/finalize_revision_state.py',
        'hermes_source_units/file_locks.py','hermes_source_units/knowledge_build.py'))).hexdigest()

def recover(adapter,request,*,dry_run=False):
    if request.get('recovery_scope')!=SCOPE or request.get('target_contract_sha256')!=contract_hash():
        fail('Build recovery requires the reviewed current implementation and exact scope')
    from reduce_execution_recovery import recover_reduce_execution
    return recover_reduce_execution(adapter,request,dry_run=dry_run)

def view_for(adapter,binding,view,assessment):
    from fixed_page_evidence import select
    from fixed_semantic import prepare_request_projection
    from semantic_output import path_for
    result=select(adapter,binding,'page-review',view,prepare_request_projection)
    if not path_for(adapter,binding,'page-review',result).exists() and assessment['risk_reasons']:
        result={**result,'review_scope':{'policy':assessment['policy'],
            'risk_reasons':assessment['risk_reasons'],
            'instruction':'Review only the named page risks against its assigned candidates, originals and parent. Do not repeat candidate discovery or review unrelated reading-package coverage.'}}
    return result

def verify(adapter,w,node,binding,entry,runs):
    import fixed_review
    from fixed_semantic import prepare_request_projection,decode_semantic_response
    from fixed_semantic_schema import schema_for
    from fixed_semantic_media import image_catalog,select_images
    from fixed_semantic_recovery import native_proof,permitted_tasks
    from semantic_output import path_for
    if w['checkpoints']['checkpoint_1']['state']!='approved':fail('Build requires the accepted checkpoint')
    if not fixed_review.policy_enabled(adapter,binding):fail('Build recovery requires the pinned program-acceptance contract')
    path=_vault_path(adapter.workflow.vault,entry['snapshot_ref']);raw=path.read_bytes();old=json.loads(raw)
    if hashlib.sha256(raw).hexdigest()!=entry['snapshot_sha256']:
        fail('original Build snapshot changed')
    permitted=permitted_tasks(adapter,binding)
    graph_proof=entry.get('_verified_graph_interruption')
    if graph_proof:
        # Rebind pure input metadata only, from an authenticated ancestor. All
        # domain bytes, decisions, registry and assessments must be identical.
        prior=old['binding']
        if (prior['task_id'] not in permitted or prior['task_id']==binding['task_id']
                or {k:v for k,v in prior.items() if k not in ('task_id','input_fingerprint')}
                   !={k:v for k,v in binding.items() if k not in ('task_id','input_fingerprint')}
                or entry.get('retry_unknowns')):
            fail('pre-execution recovery requires unchanged ancestor evidence and zero new grants')
        original=copy.deepcopy(old);original.pop('input_id')
        if fingerprint(original)!=old['input_id']:fail('ancestor Build snapshot identity differs')
        old={**old,'binding':dict(binding)};old.pop('input_id');old['input_id']=fingerprint(old)
    elif old['binding']!=binding:
        fail('original Build binding changed')
    current=fixed_review.read_snapshot(adapter,binding,{'batch_id':w['batch_id']},True)
    from hermes_source_units.partial_finalize import equivalent_snapshot
    if current!=old and not equivalent_snapshot(adapter.workflow.knowledge,old,current):
        fail('current Build evidence, drafts, parents or registry differ beyond authenticated partial commits')
    grants=entry.get('retry_unknowns',[])
    if (not isinstance(grants,list) or len({g.get('attempt_ref') for g in grants})!=len(grants)
            or any(set(g)!= {'attempt_ref','attempt_sha256','reason'} or not str(g['reason']).strip() for g in grants)):
        fail('explicit unknown-call grants need unique exact attempts and reasons')
    requested={g['attempt_ref']:g for g in grants};used=set();reviews=[];accounted=set();journals=set()
    permitted[binding['task_id']]={'ended_run_ids':[str(r['id']) for r in runs]}
    root=path.parent.parent
    by_input={}
    for jp in (root/'output-attempts').glob('*.json'):
        j=_load_json(jp)
        if j.get('phase')=='page-review':by_input.setdefault(j['input_sha256'],[]).append((jp,j))
    for original in old['views']:
        assessment=old['backend']['pages'][original['page']]['assessment']
        # Reconstruct only consumed variants. Untouched pages have no native
        # output to authenticate; admission need not pack a new model request.
        from fixed_page_evidence import MARKER,CONTRACT
        variants=[original,{**original,MARKER:CONTRACT}]
        if assessment['risk_reasons']:
            variants.extend({**v,'review_scope':{'policy':assessment['policy'],
                'risk_reasons':assessment['risk_reasons'],
                'instruction':'Review only the named page risks against its assigned candidates, originals and parent. Do not repeat candidate discovery or review unrelated reading-package coverage.'}} for v in list(variants))
        matches=[(v,*pair) for v in variants for pair in by_input.get(fingerprint(v),[])]
        if not matches:continue
        if len(matches)!=1:fail('multiple consumed representations of one review remain held')
        view,jp,journal=matches[0];schema=schema_for('page-review',view)
        if path_for(adapter,binding,'page-review',view)!=jp:fail('review journal path differs from its input identity')
        if (journal.get('contract')!='finite-semantic-output/v1' or journal.get('phase')!='page-review'
                or journal.get('input_sha256')!=fingerprint(view) or journal.get('schema_sha256')!=fingerprint(schema)):
            fail('review journal differs from current exact input/schema')
        images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence']))
        attempts=journal['attempts'];decision=None;unknown=False;unproven_stop=False
        if not attempts:fail('empty consumed review journal')
        for a in attempts:
            wire=prepare_request_projection('page-review',view,schema,images,a.get('correction'))['wire']
            if a['wire_sha256']!=fingerprint(wire) or a['task_id'] not in permitted:
                fail('review wire or native owner changed')
            if a.get('audit_ref'):
                proof=native_proof(adapter,binding,_vault_path(adapter.workflow.vault,a['audit_ref']),wire,schema,images,permitted)
                if proof is None:fail('saved review lacks exact genuine native provenance')
                accounted.add(a['audit_ref'])
                try:answer,_=decode_semantic_response(proof[0])
                except ValueError:answer={}
                if a['state']=='valid':decision=answer.get('decision')
            else:
                pointer=a.get('call_outcome',{});root=jp.parent.parent
                if not pointer:
                    # Cancellation may reject an already reserved ordinal before
                    # a native outcome is sealed. Absence of a session/outcome
                    # never proves "not called". Preserve this exact single
                    # ordinal as held, with no replay or additional-call grant.
                    owner=permitted[a['task_id']]
                    if (len(attempts)!=1 or a.get('state')!='rejected'
                            or a.get('correctable') is not False or a.get('session_id')
                            or a.get('diagnostic',{}).get('code')!='WORKFLOW_STOPPED'
                            or str(a.get('run_id')) not in owner['ended_run_ids']):
                        fail('unknown review lacks an immutable native outcome')
                    unproven_stop=True;unknown=True
                    continue
                rp=(root/pointer.get('report_ref','')).resolve()
                if (rp.parent!=root/'call-failures' or not rp.is_file()
                        or hashlib.sha256(rp.read_bytes()).hexdigest()!=pointer.get('report_sha256')):
                    fail('unknown review lacks an immutable native outcome')
                report=_load_json(rp)
                if (report.get('state')!='completion_unknown' or report.get('attempt_id')!=a['attempt_id']
                        or report.get('session_id')!=a.get('session_id') or report.get('wire')!=wire
                        or fingerprint(report.get('view'))!=fingerprint(view) or report.get('completed_output') is not False):
                    fail('unknown review identity or original evidence differs')
                unknown=True
        unknown=not bool(attempts[-1].get('audit_ref'))
        ref=jp.relative_to(adapter.workflow.vault).as_posix();grant=requested.get(ref)
        if grant:
            if (unproven_stop or not unknown or len(attempts)!=1 or attempts[0].get('state') not in ('reserved','rejected')
                    or attempts[0].get('audit_ref') or fingerprint(attempts[0])!=grant['attempt_sha256']):
                fail('one explicit extra call may target only its original unknown ordinal')
            used.add(ref)
        journals.add(jp.name)
        reviews.append({'journal_name':jp.name,'page':original['page'],'input_sha256':fingerprint(view),
            'schema_sha256':fingerprint(schema),'preserved_attempt_count':len(attempts),
            'prior_attempts_sha256':fingerprint(attempts),'decision':decision,'unknown':unknown,
            'additional_calls':1 if grant else 0})
        if unproven_stop:
            reviews[-1]['unproven_stopped_attempt']=True
    if used!=set(requested):fail('unknown-call grant does not match this Build snapshot')
    from finalize_revision import authenticate_history
    controlled_audits,controlled_journals=authenticate_history(adapter,binding,permitted)
    accounted.update(controlled_audits);journals.update(controlled_journals)
    for jp in (root/'output-attempts').glob('*.json'):
        j=_load_json(jp)
        if any(a.get('task_id')==binding['task_id'] for a in j.get('attempts',[])) and jp.name not in journals:
            fail('native owner has a review outside the current snapshot')
    for ap in (root/'semantic-calls').glob('*.json'):
        a=_load_json(ap)
        if a.get('native_task_id')==binding['task_id'] and ap.relative_to(adapter.workflow.vault).as_posix() not in accounted:
            fail('native Build call is not accounted for by its exact finite journal')
    return {**({'preexecution_graph_interruption':graph_proof} if graph_proof else {}),
        'build_review_recovery':{'contract':SCOPE,'snapshot_ref':entry['snapshot_ref'],
        'snapshot_sha256':entry['snapshot_sha256'],'input_id':current['input_id'],
        'original_attempts_preserved':True,'additional_calls_granted':len(grants),'reviews':reviews}}

def review_permit(adapter,binding,path,record):
    """Read current sealed admission; caller separately checks live worker authority."""
    from fixed_history import journal_rows
    for _,review in journal_rows(adapter,binding,path.name,'build_review',scopes=(SCOPE,),reverse=True,
            code='RECOVERY_NOT_ALLOWED',message='Build recovery report changed'):
        if not review['unknown']:continue
        count=review['preserved_attempt_count']
        if (record['input_sha256']!=review['input_sha256'] or record['schema_sha256']!=review['schema_sha256']
                or fingerprint(record['attempts'][:count])!=review['prior_attempts_sha256']
                or len(record['attempts'])>count+review['additional_calls']):fail('original unknown review or finite grant changed')
        return review
    return None

def execute_review(adapter,binding,view,caller,model_call,validator,path,record,permit):
    """Append at most the explicitly granted ordinal; never overwrite the unknown."""
    from semantic_output import ACTIVE,write,held
    from fixed_semantic import prepare_request_projection,decode_semantic_response
    from fixed_semantic_schema import schema_for
    from fixed_semantic_media import image_catalog,select_images
    from fixed_semantic_recovery import native_reply
    if permit['additional_calls']==0:held('original unknown Build review remains held; no new call authorized')
    index=permit['preserved_attempt_count'];schema=schema_for('page-review',view)
    images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence']))
    wire=prepare_request_projection('page-review',view,schema,images)['wire']
    if len(record['attempts'])>index:
        a=record['attempts'][index]
        if a['wire_sha256']!=fingerprint(wire) or a['state']!='valid' or not a.get('audit_ref'):
            held('explicit Build retry is already consumed or uncertain')
        proof=native_reply(adapter,binding,_vault_path(adapter.workflow.vault,a['audit_ref']),wire,schema,images)
        if proof is None:held('explicit Build retry lacks matching native proof')
        draft,_=decode_semantic_response(proof[0]);return draft,a['audit_ref'],validator(draft)
    adapter.worker_check(binding)
    a={'attempt':index+1,'attempt_id':fingerprint({'path':path.name,'attempt':index+1}),
        'task_id':binding['task_id'],'run_id':__import__('os').environ.get('HERMES_KANBAN_RUN_ID'),
        'wire_sha256':fingerprint(wire),'state':'reserved','correction':None,'build_unknown_retry':True}
    record['attempts'].append(a);write(path,record);token=ACTIVE.set((path,record,index))
    try:
        draft,ref=model_call(adapter,binding,'page-review',view,caller,30000,attempt_limit=1)
        result=validator(draft);a.update(state='valid',correctable=False);write(path,record)
        return draft,ref,result
    except ContractError as exc:
        a.update(state='rejected',correctable=False,diagnostic={'code':exc.code,'reason':str(exc)});write(path,record)
        raise
    finally:ACTIVE.reset(token)
