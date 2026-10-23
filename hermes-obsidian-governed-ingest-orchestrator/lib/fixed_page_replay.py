"""Authenticate existing finite page outcomes without running writing cleanup."""
import json
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path
from hermes_source_units.validation import fingerprint


def read_outcome(adapter,binding,view,prove,images):
    from semantic_output import path_for,CONTRACT,ELIGIBLE,check
    from fixed_semantic import prepare_request_projection,decode_semantic_response
    from fixed_semantic_schema import schema_for
    path=path_for(adapter,binding,'page-write',view)
    if not path.is_file():return None
    journal=_load_json(path);attempts=journal.get('attempts',[]);schema=schema_for('page-write',view)
    from reduce_disposition import output_permit,read_recovery
    permit=output_permit(adapter,binding,path,journal)
    if permit:
        if len(attempts)==permit['preserved_attempt_count']:return None
        ref=attempts[-1].get('audit_ref')
        if not ref:return None
        try:
            draft,_,_=read_recovery(adapter,binding,'page-write',view,
                lambda d:check('page-write',None,None,None,d),prove,ref,path,journal,permit)
        except ContractError:return None
        return {'kind':'valid','ref':ref,'draft':draft,'error':None}
    if (journal.get('contract')!=CONTRACT or journal.get('phase')!='page-write' or
        journal.get('input_sha256')!=fingerprint(view) or journal.get('schema_sha256')!=fingerprint(schema) or not 1<=len(attempts)<=2):return None
    last=None;previous=None
    for index,attempt in enumerate(attempts):
        if (attempt.get('attempt')!=index+1 or attempt.get('attempt_id')!=fingerprint({'path':path.name,'attempt':index+1})
            or index and (previous is None or previous.code not in ELIGIBLE)):return None
        ref=attempt.get('audit_ref')
        if not ref:
            return {'kind':'pending','ref':last['ref'],'error':previous} if last and previous else None
        prepared=prepare_request_projection('page-write',view,schema,images,attempt.get('correction'))
        if prepared['input_size']>30000 or attempt.get('wire_sha256')!=fingerprint(prepared['wire']):return None
        audit_path=_vault_path(adapter.workflow.vault,ref);audit=_load_json(audit_path)
        if (audit.get('phase')!='page-write' or audit.get('native_task_id')!=attempt.get('task_id')
            or str(audit.get('native_run_id'))!=str(attempt.get('run_id'))
            or audit.get('output_attempt_id',attempt['attempt_id'])!=attempt['attempt_id']
            or attempt.get('session_id') and audit.get('model',{}).get('session_id')!=attempt['session_id']):return None
        proof=prove(audit_path,prepared['wire'],schema,images)
        if proof is None:return None
        error=None;draft=None
        try:
            try:draft,_=decode_semantic_response(proof[0])
            except json.JSONDecodeError as exc:raise ContractError('INVALID_SCHEMA','$',str(exc))
            check('page-write',None,None,None,draft)
        except ContractError as exc:error=exc
        if error is None:
            if index!=len(attempts)-1 or attempt.get('state')!='valid':return None
            last={'kind':'valid','ref':ref,'draft':draft,'error':None}
        else:
            if attempt.get('state')!='rejected':return None
            if index<len(attempts)-1 and (error.code not in ELIGIBLE or not attempt.get('correctable')):return None
            last={'kind':'held','ref':ref,'error':error}
        previous=error
    if last['kind']=='held' and len(attempts)==1 and attempts[0].get('correctable'):return None
    return last


def restore(adapter,binding,value,state,caller,project,prove):
    """Pure checked set restoration. Caller owns current authority boundaries."""
    from fixed_semantic_recovery import permitted_tasks
    from fixed_page_work import measure
    from fixed_semantic_media import image_catalog,select_images
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}"
    allowed=permitted_tasks(adapter,binding);written_by={};held_by={}
    for path in sorted((root/'semantic-inputs').glob('page-work-*.json'),key=lambda p:p.stat().st_mtime):
        old=_load_json(path);b=old.get('binding',{})
        if (old.get('contract')!='native-page-work/v1' or b.get('workflow_id')!=binding['workflow_id']
            or b.get('node')!=binding['node'] or b.get('task_id') not in allowed):continue
        for row in old.get('written',[]):written_by[row['path']]=row
        for row in old.get('held',[]):held_by[row['path']]=row
    written=[];held=[];done=set()
    for page in state['pages']:
        from reduce_disposition import page_permit
        permission=page_permit(adapter,binding,page)
        if permission and permission['action'] in ('revise-page-plan','exclude-covered'):continue
        row=written_by.get(page['path']) or held_by.get(page['path'])
        if not row or row.get('candidate_refs')!=page['candidate_refs']:continue
        view,size=measure(adapter,value,page,caller,project)
        if size>30000:continue
        images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence'])) if 'original_evidence' in view else []
        result=read_outcome(adapter,binding,view,prove,images)
        if result is None or result['ref']!=row.get('audit_ref'):continue
        if 'draft_ref' in row:
            expected=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/page-drafts/{fingerprint(view)}.md"
            if (result['kind']!='valid' or row.get('identity')!=page['identity'] or row.get('input_sha256')!=fingerprint(view)
                or row['draft_ref']!=expected):continue
            content=result['draft']['content']
            if _vault_path(adapter.workflow.vault,expected).read_bytes()!=content.encode('utf-8'):
                raise ContractError('SOURCE_CHANGED','$','saved native body differs from its authenticated output')
            written.append({**row,'content':content})
        elif result['kind'] in ('held','pending') and row.get('phase')=='page-write':
            # A later uncertain reservation never revives an already rejected page.
            if result['error'] is None or result['error'].code!=row.get('code'):continue
            held.append(row)
        else:continue
        done.add(page['path'])
    return written,held,done
