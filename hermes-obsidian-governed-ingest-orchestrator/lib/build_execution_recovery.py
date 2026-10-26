"""Stopped Build admission and exact review reuse; no implicit semantic retries."""
import copy,hashlib,json
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path,_json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint

SCOPE='build-native/v1'

def fail(message):raise ContractError('RECOVERY_NOT_ALLOWED','$',message)

def contract_hash():
    root=Path(__file__).parent
    return hashlib.sha256(b''.join((root/n).read_bytes() for n in (
        'build_execution_recovery.py','reduce_execution_recovery.py','fixed_review.py',
        'semantic_output.py','fixed_semantic.py','native_call_state.py'))).hexdigest()

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
    if hashlib.sha256(raw).hexdigest()!=entry['snapshot_sha256'] or old['binding']!=binding:
        fail('original Build snapshot or binding changed')
    current=fixed_review.read_snapshot(adapter,binding,{'batch_id':w['batch_id']},True)
    if current!=old:fail('current Build evidence, drafts, parents or registry differ')
    grants=entry.get('retry_unknowns',[])
    if (not isinstance(grants,list) or len({g.get('attempt_ref') for g in grants})!=len(grants)
            or any(set(g)!= {'attempt_ref','attempt_sha256','reason'} or not str(g['reason']).strip() for g in grants)):
        fail('explicit unknown-call grants need unique exact attempts and reasons')
    requested={g['attempt_ref']:g for g in grants};used=set();reviews=[];accounted=set();journals=set()
    permitted=permitted_tasks(adapter,binding);permitted[binding['task_id']]={'ended_run_ids':[str(r['id']) for r in runs]}
    root=path.parent.parent
    by_input={}
    for jp in (root/'output-attempts').glob('*.json'):
        j=_load_json(jp)
        if j.get('phase')=='page-review':by_input.setdefault(j['input_sha256'],[]).append((jp,j))
    for original in current['views']:
        assessment=current['backend']['pages'][original['page']]['assessment']
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
        attempts=journal['attempts'];decision=None;unknown=False
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
            if (not unknown or len(attempts)!=1 or attempts[0].get('state') not in ('reserved','rejected')
                    or attempts[0].get('audit_ref') or fingerprint(attempts[0])!=grant['attempt_sha256']):
                fail('one explicit extra call may target only its original unknown ordinal')
            used.add(ref)
        journals.add(jp.name)
        reviews.append({'journal_name':jp.name,'page':original['page'],'input_sha256':fingerprint(view),
            'schema_sha256':fingerprint(schema),'preserved_attempt_count':len(attempts),
            'prior_attempts_sha256':fingerprint(attempts),'decision':decision,'unknown':unknown,
            'additional_calls':1 if grant else 0})
    if used!=set(requested):fail('unknown-call grant does not match this Build snapshot')
    for jp in (root/'output-attempts').glob('*.json'):
        j=_load_json(jp)
        if any(a.get('task_id')==binding['task_id'] for a in j.get('attempts',[])) and jp.name not in journals:
            fail('native owner has a review outside the current snapshot')
    for ap in (root/'semantic-calls').glob('*.json'):
        a=_load_json(ap)
        if a.get('native_task_id')==binding['task_id'] and ap.relative_to(adapter.workflow.vault).as_posix() not in accounted:
            fail('native Build call is not accounted for by its exact finite journal')
    return {'build_review_recovery':{'contract':SCOPE,'snapshot_ref':entry['snapshot_ref'],
        'snapshot_sha256':entry['snapshot_sha256'],'input_id':current['input_id'],
        'original_attempts_preserved':True,'additional_calls_granted':len(grants),'reviews':reviews}}

def review_permit(adapter,binding,path,record):
    """Read current sealed admission; caller separately checks live worker authority."""
    w=adapter.workflow.status(binding['workflow_id'])
    for pointer in reversed(w.get('execution_recovery_history',[])):
        rp=_vault_path(adapter.workflow.vault,pointer['report_ref']);raw=rp.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=pointer['report_sha256']:fail('Build recovery report changed')
        report=json.loads(raw)
        if report['request'].get('recovery_scope')!=SCOPE:continue
        for entry in report['entries']:
            if entry['node']!=binding['node']:continue
            for review in entry['build_review_recovery']['reviews']:
                if review['journal_name']!=path.name or not review['unknown']:continue
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
