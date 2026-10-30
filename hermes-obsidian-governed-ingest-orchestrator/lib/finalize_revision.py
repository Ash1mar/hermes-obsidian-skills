"""One operator-bounded local native revision; cumulative ordinals never reset."""
import copy
from contextvars import ContextVar
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_guard import worker_binding,workflow_write_guard
from hermes_source_units.finalize_revision_state import load,append,work_id,publish_revision,fail
from hermes_source_units.partial_finalize import pin,pinned

ACTIVE=ContextVar('finalize_local_native_call',default=None)

def work_for(adapter,binding,value,view):
    row=value['backend']['pages'][view['page']]
    return load(adapter.workflow.vault,binding['workflow_id'])['works'].get(work_id(row['run_id'],row['page']['page_id']))

def marker(work):
    grant=work['grants'][-1]
    return {'instruction':'Inspect only this explicitly reopened page issue, preserve assigned evidence/identity/coverage and retain unresolved limits. '+grant['basis']['reason'],
        'basis_kind':grant['basis']['kind'],'predecessor_revision_id':work['original_revision_id']}

def view_with_control(view,work):
    if not work or not work.get('grants'):return view
    encoding=work['grants'][-1].get('evidence_encoding')
    scope=work['grants'][-1].get('review_scope')
    return {**view,'finalize_revision':marker(work),**({'evidence_encoding':encoding} if encoding else {}),
        **({'review_scope':scope} if scope else {})}

def allowed(adapter,binding,phase,view):
    token=ACTIVE.get()
    return bool(token and token['fresh'] and token['workflow_id']==binding['workflow_id']
        and token['phase']==phase and token['input_sha256']==fingerprint(view))

def update(adapter,binding,work,operation,kind,metadata):
    with worker_binding(dict(binding)),workflow_write_guard(adapter.workflow.vault,kinds=('build-finalize',),actor=binding['actor']):
        return append(adapter.workflow.vault,binding['workflow_id'],work,operation,kind,metadata)

def execute_call(adapter,binding,work,phase,view,caller,model_call):
    from semantic_output import execute,check,path_for
    vault=adapter.workflow.vault;wid=binding['workflow_id'];key=work['work_id']
    work=copy.deepcopy(load(vault,wid)['works'][key]);grant=work['grants'][-1]
    if not grant['limits'].get(phase):fail('this phase has no local revision allowance')
    old=grant['calls'].get(phase);fresh=old is None
    path=path_for(adapter,binding,phase,view)
    certificate=work.get('certificate')
    if old and certificate and certificate.get('phase','page-review')==phase and certificate['input_sha256']==fingerprint(view):
        result,audit,_=verify_certificate(adapter,binding,view,certificate)
        old.update(state='valid',audit=certificate['audit'],response=result)
        update(adapter,binding,work,'certified-'+grant['grant_id']+'-'+phase,'certified',{'phase':phase,'audit':certificate['audit']})
        return result,audit
    if old:
        if old['state']=='held':fail('local call remains held; new completion proof is required, not another call','SEMANTIC_OUTPUT_HELD')
        envelope=pinned(vault,old['request'])
        if envelope['view']!=view or envelope['phase']!=phase:fail('reserved local revision input changed; no allowance reset')
        if not path.exists():fail('reserved call lacks completion proof; no new call','SEMANTIC_OUTPUT_HELD')
    else:
        from finalize_disposition_control import material_identity
        if material_identity(view)!=grant['material_sha256']:
            fail('local revision material differs from its operator authorization','STALE_INPUT')
        envelope={'phase':phase,'view':view,'work_id':key,'grant_id':grant['grant_id']}
        ref=f"_system/ledgers/ingest-workflows/{wid}/finalize-controls/requests/{fingerprint(envelope)}.json"
        _write_atomic(_vault_path(vault,ref),_json_bytes(envelope))
        grant['calls'][phase]={'request':pin(vault,ref),'state':'reserved','audit':None,
            'journal_ref':path.relative_to(vault).as_posix(),'cumulative_ordinal':
                work['baseline_calls'].get(phase,0)+sum(phase in g['calls'] for g in work['grants'])+1}
        update(adapter,binding,work,'reserve-'+grant['grant_id']+'-'+phase,'reserve',{'input_sha256':fingerprint(view),'phase':phase})
    token=ACTIVE.set({'fresh':fresh,'workflow_id':wid,'phase':phase,'input_sha256':fingerprint(view)})
    try:
        result,audit,_=execute(adapter,binding,phase,view,caller,model_call,
            lambda d:check(phase,None,None,None,d),attempt_limit=1)
    except ContractError as exc:
        current=copy.deepcopy(load(vault,wid)['works'][key]);call=current['grants'][-1]['calls'][phase]
        call['state']='held';call['diagnostic']={'code':exc.code,'reason':str(exc)}
        ref=getattr(exc,'semantic_audit_ref',None)
        if ref:call['audit']=pin(vault,ref)
        # Same stopped/uncertain phase never gets another ordinal, even if a new
        # task, operation or cosmetic request representation is introduced.
        update(adapter,binding,current,'outcome-'+grant['grant_id']+'-'+phase,'outcome',{'phase':phase,'state':'held','diagnostic':call['diagnostic']})
        raise
    finally:ACTIVE.reset(token)
    current=copy.deepcopy(load(vault,wid)['works'][key]);call=current['grants'][-1]['calls'][phase]
    call.update(state='valid',audit=pin(vault,audit),response=result)
    update(adapter,binding,current,'outcome-'+grant['grant_id']+'-'+phase,'outcome',{'phase':phase,'state':'valid','audit':call['audit']})
    return result,audit

def prepare_revisions(adapter,binding,value,caller,model_call):
    changed=False
    for view in value['views']:
        work=work_for(adapter,binding,value,view)
        if not work or not work.get('grants') or (work['state'] not in ('revision_authorized','revision_authored') and not (work['state']=='open' and work.get('certificate'))):continue
        grant=work['grants'][-1]
        if any(c['state']=='held' and not (work.get('certificate',{}).get('phase')==p) for p,c in grant['calls'].items()):continue
        try:
            current_view=view_with_control(view,work)
            if grant['mode']=='rewrite_and_review' and work.get('authored_grant_id')!=grant['grant_id']:
                answer,audit=execute_call(adapter,binding,work,'page-write',current_view,caller,model_call)
                work=copy.deepcopy(load(adapter.workflow.vault,binding['workflow_id'])['works'][work['work_id']])
                call=work['grants'][-1]['calls']['page-write']
                with worker_binding(dict(binding)),workflow_write_guard(adapter.workflow.vault,kinds=('build-finalize',),actor=binding['actor']):
                    work['latest_revision']=publish_revision(adapter.workflow.knowledge,binding,work,answer['content'],pin(adapter.workflow.vault,audit),call['request'])
                    work['state']='revision_authored';work['authored_grant_id']=grant['grant_id']
                    append(adapter.workflow.vault,binding['workflow_id'],work,'revision-'+grant['grant_id'],'revision',{'revision':work['latest_revision']})
                if work['latest_revision']:
                    record=pinned(adapter.workflow.vault,work['latest_revision'])
                    current_view['content']=_vault_path(adapter.workflow.vault,record['content']['ref']).read_text(encoding='utf-8')
                changed=True
            result,audit=execute_call(adapter,binding,work,'page-review',current_view,caller,model_call)
            work=copy.deepcopy(load(adapter.workflow.vault,binding['workflow_id'])['works'][work['work_id']])
            work['state']='revision_reviewed';work['review_result']=result;work['review_audit']=pin(adapter.workflow.vault,audit)
            update(adapter,binding,work,'reviewed-'+grant['grant_id'],'reviewed',{'decision':result['decision'],'audit':work['review_audit']})
            changed=True
        except ContractError as exc:
            from fixed_page_work import LOCAL
            if exc.code not in LOCAL:raise
    return changed

def controlled_result(adapter,binding,value,view):
    work=work_for(adapter,binding,value,view)
    if not work:return None
    if work.get('certificate') and work['certificate'].get('phase','page-review')=='page-review' and work['certificate']['input_sha256']==fingerprint(view):
        return verify_certificate(adapter,binding,view,work['certificate'])
    if not work.get('grants'):return None
    grant=work['grants'][-1];call=grant['calls'].get('page-review')
    if work['state']!='revision_reviewed' or not call or call['state']!='valid':
        raise ContractError('SEMANTIC_OUTPUT_HELD','$','local revision is incomplete, rejected or uncertain; preserve its cumulative ordinals')
    from fixed_semantic_recovery import native_reply
    from fixed_semantic import prepare_request_projection,decode_semantic_response
    from fixed_semantic_schema import schema_for
    from fixed_semantic_media import image_catalog,select_images
    exact=pinned(adapter.workflow.vault,call['request'])['view']
    if exact!=view:fail('controlled review differs from current revised page','STALE_INPUT')
    schema=schema_for('page-review',view);images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence']))
    wire=prepare_request_projection('page-review',view,schema,images)['wire']
    proof=native_reply(adapter,binding,_vault_path(adapter.workflow.vault,call['audit']['ref']),wire,schema,images)
    if proof is None:fail('controlled review lacks exact native completion proof')
    result,_=decode_semantic_response(proof[0]);return result,call['audit']['ref'],result

def certify_existing(adapter,snapshot,item,basis,*,work=None):
    from build_execution_recovery import view_for
    from fixed_semantic_recovery import native_proof,permitted_tasks
    from fixed_semantic import prepare_request_projection,decode_semantic_response
    from fixed_semantic_schema import schema_for
    from fixed_semantic_media import image_catalog,select_images
    view=next(v for v in snapshot['views'] if v['page']==item['page']);binding=snapshot['binding']
    audit_pointer=basis.get('native_audit')
    if not audit_pointer or audit_pointer not in basis['evidence']:fail('exact existing native audit must be pinned as new evidence')
    audit=pinned(adapter.workflow.vault,audit_pointer)
    phase=audit.get('phase')
    controlled=work and work.get('grants') and work['grants'][-1]['calls'].get(phase)
    if controlled:
        view=pinned(adapter.workflow.vault,controlled['request'])['view']
        journal_pointer=pin(adapter.workflow.vault,controlled['journal_ref'])
    else:
        if phase!='page-review':fail('existing completion has no authorized controlled phase')
        view=view_for(adapter,binding,view,snapshot['backend']['pages'][view['page']]['assessment'])
        journal_pointer=item['history']['journal']
    journal=pinned(adapter.workflow.vault,journal_pointer);attempt=journal['attempts'][-1]
    if audit.get('output_attempt_id')!=attempt['attempt_id']:fail('existing completion is for a different ordinal')
    schema=schema_for(phase,view);images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence']))
    wire=prepare_request_projection(phase,view,schema,images,attempt.get('correction'))['wire']
    proof=native_proof(adapter,binding,_vault_path(adapter.workflow.vault,audit_pointer['ref']),wire,schema,images,permitted_tasks(adapter,binding))
    if proof is None:fail('existing completion cannot be authenticated')
    result,_=decode_semantic_response(proof[0])
    from semantic_output import check
    check(phase,None,None,None,result)
    return {'phase':phase,'view':view,'audit':audit_pointer,'journal':journal_pointer,'input_sha256':fingerprint(view),'result':result}

def verify_certificate(adapter,binding,view,certificate):
    from fixed_semantic_recovery import native_reply
    from fixed_semantic import prepare_request_projection,decode_semantic_response
    from fixed_semantic_schema import schema_for
    from fixed_semantic_media import image_catalog,select_images
    if fingerprint(view)!=certificate['input_sha256']:fail('certified completion input changed','STALE_INPUT')
    journal=pinned(adapter.workflow.vault,certificate['journal'])
    audit=pinned(adapter.workflow.vault,certificate['audit'])
    if audit.get('output_attempt_id')!=journal['attempts'][-1]['attempt_id']:fail('certified ordinal changed')
    phase=certificate.get('phase','page-review')
    schema=schema_for(phase,view);images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence']))
    wire=prepare_request_projection(phase,view,schema,images,journal['attempts'][-1].get('correction'))['wire']
    proof=native_reply(adapter,binding,_vault_path(adapter.workflow.vault,certificate['audit']['ref']),wire,schema,images)
    if proof is None:fail('existing native completion proof no longer matches')
    result,_=decode_semantic_response(proof[0]);return result,certificate['audit']['ref'],result

def authenticate_history(adapter,binding,permitted):
    """Account for controlled ordinals on cancellation without granting replay."""
    from fixed_semantic_recovery import native_proof
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_schema import schema_for
    from fixed_semantic_media import image_catalog,select_images
    audits=set();journals=set();vault=adapter.workflow.vault
    for work in load(vault,binding['workflow_id'])['works'].values():
        if work.get('certificate'):
            certificate=work['certificate'];view=certificate['view'];phase=certificate['phase']
            journal=pinned(vault,certificate['journal']);audit=pinned(vault,certificate['audit'])
            if audit.get('output_attempt_id')!=journal['attempts'][-1]['attempt_id']:fail('certified original ordinal changed')
            schema=schema_for(phase,view);images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence']))
            wire=prepare_request_projection(phase,view,schema,images,journal['attempts'][-1].get('correction'))['wire']
            if native_proof(adapter,binding,_vault_path(vault,certificate['audit']['ref']),wire,schema,images,permitted) is None:
                fail('existing completion certificate lost its native proof')
            audits.add(certificate['audit']['ref'])
        for grant in work.get('grants',[]):
            for phase,call in grant['calls'].items():
                envelope=pinned(vault,call['request']);view=envelope['view']
                if envelope['work_id']!=work['work_id'] or envelope['grant_id']!=grant['grant_id'] or envelope['phase']!=phase:
                    fail('controlled call envelope differs')
                jp=_vault_path(vault,call['journal_ref'])
                if not jp.exists():continue  # Reserved before finite journal creation: stays uncertain, never replayed.
                record=_load_json(jp);journals.add(jp.name)
                if record['input_sha256']!=fingerprint(view) or len(record['attempts'])>grant['limits'][phase]:
                    fail('controlled call exceeds its stable cumulative allowance')
                schema=schema_for(phase,view);images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence']))
                for attempt in record['attempts']:
                    wire=prepare_request_projection(phase,view,schema,images,attempt.get('correction'))['wire']
                    if attempt['wire_sha256']!=fingerprint(wire):fail('controlled wire changed')
                    if attempt.get('audit_ref'):
                        if native_proof(adapter,binding,_vault_path(vault,attempt['audit_ref']),wire,schema,images,permitted) is None:
                            fail('controlled ordinal lacks native provenance')
                        audits.add(attempt['audit_ref'])
                    elif attempt.get('call_outcome'):
                        pointer=attempt['call_outcome'];path=jp.parent.parent/pointer['report_ref']
                        report=pinned(vault,{'ref':path.relative_to(vault).as_posix(),'sha256':pointer['report_sha256']})
                        if report.get('wire')!=wire or report.get('attempt_id')!=attempt['attempt_id'] or report.get('completed_output') is not False:
                            fail('controlled unknown outcome differs')
                    elif attempt.get('diagnostic',{}).get('code')!='WORKFLOW_STOPPED':
                        fail('controlled ordinal has no authenticated completion or stop evidence')
    return audits,journals
