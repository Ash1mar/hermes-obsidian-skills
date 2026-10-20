"""Shared fixed-call outcomes; no semantic approval or automatic new allowance."""
import copy,hashlib,json,os
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint

CONTRACT='native-call-outcome/v1'


class StopNativeCall(BaseException):
    """Escape the SDK's blanket Exception retry loop; caught at fixed boundary."""
    def __init__(self,outcome):self.outcome=outcome
    def __str__(self):return self.outcome['error']['message']


def classify(error):
    body=getattr(error,'body',None) or {};body=body if isinstance(body,dict) else {}
    detail=body.get('error',body);detail=detail if isinstance(detail,dict) else {}
    status=getattr(error,'status_code',None) or detail.get('status_code')
    kind=detail.get('type') or getattr(error,'type',None)
    partial=bool(getattr(error,'response_id',None) or body.get('output') or body.get('partial'))
    rejected=not partial and (status in (400,401,403,404,405,413,415,422) or kind in
        ('invalid_request_error','authentication_error','permission_error','not_found_error'))
    retryable=not partial and status==429
    return {'contract':CONTRACT,'state':'request_rejected' if rejected or retryable else 'completion_unknown',
        'retryable':retryable,'error':{'class':type(error).__name__,'type':kind,'status_code':status,
        'code':detail.get('code'),'param':detail.get('param'),'message':str(detail.get('message') or error)[:2048]}}


def request_identity(api_kwargs):
    # No credentials/headers: this is the SDK payload, not an HTTP client config.
    return hashlib.sha256(json.dumps(api_kwargs,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def record(outcome,prepared,session,*,request_sha256=None,output_format=None):
    """Append evidence, leave the original ordinal/reservation untouched."""
    from semantic_output import ACTIVE,write
    active=ACTIVE.get()
    if active is not None:
        path,journal,index=active;attempt=journal['attempts'][index];root=path.parent.parent
    else:
        context=prepared.get('audit_context')
        if not context:raise ContractError('NATIVE_COMPLETION_UNKNOWN','$','fixed call lacks durable native context')
        binding=context['binding'];root=Path(context['vault'])/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}"
        journal={'phase':context['phase'],'schema_sha256':fingerprint(prepared['identity']['output_schema'])}
        attempt={'attempt_id':None,'task_id':binding['task_id'],'run_id':os.environ.get('HERMES_KANBAN_RUN_ID')}
    report={**copy.deepcopy(outcome),'session_id':session,'attempt_id':attempt['attempt_id'],
        'task_id':attempt['task_id'],'run_id':attempt.get('run_id'),'phase':journal['phase'],
        'wire_sha256':fingerprint(prepared['wire']),'schema_sha256':journal['schema_sha256'],
        'wire':copy.deepcopy(prepared['wire']),
        'view':copy.deepcopy(prepared['identity']['view']) if prepared.get('identity') else None,
        'image_inputs':[{k:v for k,v in i.items() if k!='url'} for i in prepared.get('images',[])],
        'final_request_sha256':request_sha256,'output_format':output_format,
        'completed_output':False,'extra_calls_granted':0}
    ref=root/'call-failures'/(fingerprint(report)+'.json')
    data=_json_bytes(report)
    if ref.exists() and ref.read_bytes()!=data:raise ContractError('SOURCE_CHANGED','$','call failure evidence changed')
    if not ref.exists():_write_atomic(ref,data)
    if active is not None:
        attempt['call_outcome']={'state':report['state'],'report_ref':ref.relative_to(root).as_posix(),'report_sha256':hashlib.sha256(data).hexdigest()}
        # Workflow-root-relative; recovery derives that same root.
        write(path,journal)
    code='NATIVE_REQUEST_REJECTED' if report['state']=='request_rejected' else 'NATIVE_COMPLETION_UNKNOWN'
    raise ContractError(code,'$',json.dumps({'state':report['state'],'session_id':session,'report':str(ref),
        'reason':report['error']['message']},ensure_ascii=False))


def recovery_evidence(adapter,workflow,binding,runs):
    """Existing recovery admission: authenticate calls, never restart their ordinals."""
    import sqlite3
    from types import SimpleNamespace
    from hermes_source_units.source_units import _load_json
    from fixed_semantic_media import normalize_content
    from fixed_semantic_recovery import _request_text
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}"
    if not runs or any(not r.get('ended_at') for r in runs):raise ContractError('WORKER_ACTIVE','$','native run is not ended')
    ended={str(r['id']) for r in runs};outcomes=[];accounted=set()
    with sqlite3.connect('file:'+str(Path.home()/'.hermes/state.db')+'?mode=ro',uri=True) as db:
      for path in sorted((root/'output-attempts').glob('*.json')):
        journal=_load_json(path)
        accounted.update(a['audit_ref'] for a in journal.get('attempts',[]) if a.get('audit_ref'))
        for attempt in journal.get('attempts',[]):
            if attempt.get('task_id')!=binding['task_id'] or attempt.get('audit_ref'):continue
            if (journal.get('contract')!='finite-semantic-output/v1' or str(attempt.get('run_id')) not in ended
                or attempt.get('state') not in ('reserved','rejected') or not attempt.get('session_id')):
                raise ContractError('RECOVERY_NOT_ALLOWED','$','unaccounted or active native reservation')
            sid=attempt['session_id']
            if db.execute('SELECT source FROM sessions WHERE id=?',(sid,)).fetchone()!=('kanban',):
                raise ContractError('RECOVERY_NOT_ALLOWED','$','failure session is not native')
            messages=db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?',(sid,)).fetchall()
            if any(t and t!='[]' for _,_,t in messages):raise ContractError('RECOVERY_NOT_ALLOWED','$','failure session used tools')
            pointer=attempt.get('call_outcome');legacy=False
            if pointer:
                rp=(root/pointer['report_ref']).resolve()
                if rp.parent!=(root/'call-failures').resolve() or hashlib.sha256(rp.read_bytes()).hexdigest()!=pointer['report_sha256']:
                    raise ContractError('RECOVERY_NOT_ALLOWED','$','call failure evidence changed')
                report=_load_json(rp);wire=report['wire'];state=report['state'];image_inputs=report.get('image_inputs',[])
                if (report.get('contract')!=CONTRACT or report.get('session_id')!=sid
                    or report.get('attempt_id')!=attempt['attempt_id'] or report.get('task_id')!=binding['task_id']
                    or str(report.get('run_id')) not in ended or report.get('completed_output') is not False
                    or report.get('schema_sha256')!=journal['schema_sha256']
                    or report.get('phase')!=journal['phase'] or fingerprint(report.get('view'))!=journal['input_sha256']
                    or state not in ('request_rejected','completion_unknown')):
                    raise ContractError('RECOVERY_NOT_ALLOWED','$','call outcome identity differs')
            else:
                # Old guarded SDK dumps retain exact failed payloads and provider error
                # objects. No message-substring or zero-token inference is accepted.
                matches=[]
                for rp in (Path.home()/'.hermes/sessions').glob('request_dump_'+sid+'_*.json'):
                    dump=_load_json(rp);payload=dump.get('request',{}).get('body',{})
                    if dump.get('session_id')!=sid or not isinstance(payload,dict):continue
                    error=dump.get('error',{});state0=classify(SimpleNamespace(body=error.get('body',error),status_code=error.get('status_code')))
                    if state0['state']!='request_rejected' or state0['retryable']:continue
                    if 'instructions' in payload:
                        inputs=payload.get('input',[]);system=payload['instructions']
                    else:
                        inputs=[r for r in payload.get('messages',[]) if r.get('role')!='system']
                        systems=[r['content'] for r in payload.get('messages',[]) if r.get('role')=='system']
                        if len(systems)!=1:continue
                        system=systems[0]
                    if len(inputs)!=1 or inputs[0].get('role')!='user':continue
                    normalized=normalize_content(inputs[0].get('content'))
                    if normalized is None:continue
                    wire0={'system':system,'user':normalized[0]}
                    if fingerprint(wire0)!=attempt.get('wire_sha256'):continue
                    body,end=json.JSONDecoder().raw_decode(_request_text(wire0['user']))
                    if (fingerprint(body.get('output_schema'))!=journal['schema_sha256'] or body.get('phase')!=journal['phase']
                        or fingerprint(body.get('input'))!=journal['input_sha256']):continue
                    matches.append((rp,wire0,state0,[{} for _ in normalized[1]]))
                if len(matches)!=1:raise ContractError('RECOVERY_NOT_ALLOWED','$','no unique structured native rejection proof')
                rp,wire,status,image_inputs=matches[0];state=status['state'];legacy=True
            if fingerprint(wire)!=attempt.get('wire_sha256'):
                raise ContractError('RECOVERY_NOT_ALLOWED','$','original request hash differs')
            from revision_disposition import native_user
            audit={'user_sha256':fingerprint(wire['user']),'image_inputs':image_inputs}
            users=[native_user(c,audit) for r,c,_ in messages if r=='user']
            if users.count(wire['user'])!=1:raise ContractError('RECOVERY_NOT_ALLOWED','$','native request transcript differs')
            # Legacy synthetic failure placeholders are not genuine model outputs.
            if legacy and any(c!='Your request was not processed. Send it again if you still want me to carry it out.' for r,c,_ in messages if r=='assistant'):
                raise ContractError('RECOVERY_NOT_ALLOWED','$','possible completed output remains unclassified')
            outcomes.append({'journal_name':path.name,'journal_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'attempt_id':attempt['attempt_id'],'ordinal':attempt['attempt'],'session_id':sid,'state':state,
                'evidence_path':str(rp),'evidence_sha256':hashlib.sha256(rp.read_bytes()).hexdigest(),
                'legacy_native_dump':legacy,'same_attempt_retry_authorized':False})
    if not outcomes:raise ContractError('RECOVERY_NOT_ALLOWED','$','program failure lacks authenticated call outcomes')
    for path in (root/'semantic-calls').glob('*.json'):
        audit=_load_json(path)
        if (audit.get('native_task_id')==binding['task_id'] and not audit.get('reused_from_audit')
            and path.relative_to(adapter.workflow.vault).as_posix() not in accounted):
            raise ContractError('RECOVERY_NOT_ALLOWED','$','native output lacks its finite journal')
    return {'native_call_outcomes':outcomes,'reserved_attempts_preserved':True,'new_calls_granted':0,
        'decision':'fresh native owner may continue missing work; original rejected/unknown reservations remain held'}
