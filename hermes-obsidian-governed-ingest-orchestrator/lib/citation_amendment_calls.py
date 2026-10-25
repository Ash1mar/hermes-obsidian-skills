"""Native calls charged to stable logical lineages before dispatch."""
import copy
import json
import sqlite3
import os
from pathlib import Path
from contextvars import ContextVar
from contextlib import contextmanager
from hermes_source_units.citation_amendment import state,plan,reserve,update_call,pending_calls,fail,claim_call
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint

JOB=ContextVar('citation_amendment_job',default=None)
CALL=ContextVar('citation_amendment_call',default=None)


def active(adapter,binding):
    value=state(adapter.workflow.vault,binding['workflow_id'])
    return bool(value and value['phase']!='complete')


def check_dispatch(adapter,binding,phase,view,prepared):
    """Check the actual consumed projection, not a second reconstruction."""
    reservation=CALL.get()
    if not reservation:
        if active(adapter,binding):fail('active amendment has no logical request reservation')
        return
    identity=prepared['identity']
    if identity['phase']!=phase or identity['view']!=view or prepared['closed'] or prepared['consumed']:
        fail('reserved final request changed before dispatch')
    from fixed_semantic import _normalized_request_content
    if ({**prepared['prompt'],'user':_normalized_request_content(prepared['content'])[0]}!=prepared['wire']):
        fail('actual request content differs from the reserved final request')
    adapter.worker_check(binding)
    claim_call(reservation,prepared['wire'],identity['output_schema'],binding,
               os.environ.get('HERMES_KANBAN_RUN_ID'))

def guidance_equivalent(old,new,guidance):
    """Only expressly authorized review guidance may differ; no new evidence."""
    from fixed_semantic_recovery import _request_text
    if {k:v for k,v in old.items() if k!='user'}!={k:v for k,v in new.items() if k!='user'}:return False
    try:left=json.loads(_request_text(old['user']));right=json.loads(_request_text(new['user']))
    except (ValueError,KeyError):return False
    left=copy.deepcopy(left);right=copy.deepcopy(right)
    if right.get('input',{}).pop('amendment_review_guidance',None)!=guidance:return False
    left.get('input',{}).pop('amendment_review_guidance',None)
    return left==right

def native_home():return Path.home()/'.hermes'


def execution_record(record):
    if not record.get('execution_binding'):return record
    return {**record,'binding':record['execution_binding'],'native_run_id':record['execution_run_id']}


def execution_status(adapter,wid,node):
    """Read-only admission shared by operator recovery and execution validation."""
    value=state(adapter.workflow.vault,wid);rows=[]
    for logical,job_record in pending_calls(adapter.workflow.vault,wid)['jobs'].items():
        owned=[(i,r) for i,r in enumerate(job_record['attempts']) if r['operation_id']==value['operation_id']
               and r['binding']['node']==node]
        if not owned:continue
        index,raw=owned[-1];r=execution_record(raw)
        snap=adapter.kanban.task_snapshot(adapter.workflow.status(wid)['kanban']['board_id'],r['binding']['task_id'])
        running=bool(snap['task'].get('worker_pid') or any(not x.get('ended_at') for x in snap['runs']))
        status=('original_execution_active' if running else 'result_requires_authentication' if r.get('audit_ref') else
                'session_requires_reconciliation' if r.get('session_id') else
                'reserved_not_dispatched' if r['state']=='reserved' and raw.get('events') and
                    not any(e.get('state')=='dispatch_claimed' for e in raw['events']) else 'completion_unknown')
        if status=='result_requires_authentication':
            proof(adapter,r['binding'],r,require_stopped=True);status='authenticated_result'
        original_state=status
        grants=value.get('retry_grants',{}).get(logical,[])
        if not running and grants and grants[-1]['supersedes_attempt_sha256']==fingerprint(raw):
            from hermes_source_units.citation_amendment import plan,lineage_limits
            frozen=plan(adapter.workflow.vault,value);ledger=pending_calls(adapter.workflow.vault,wid)
            limits=lineage_limits(ledger,frozen['selection']['task_id'],frozen['cumulative_limits'])
            spent=sum(x['lineage']==raw['lineage'] and x['phase']==raw['phase']
                      for j in ledger['jobs'].values() for x in j['attempts'])
            if spent<limits[raw['phase']]:status='controlled_regeneration_authorized'
        rows.append({'logical_work':logical,'ordinal':index,'state':status,'reservation_sha256':fingerprint(raw),
                     'prior_execution_state':original_state,
                     'ended_run_ids':[str(x['id']) for x in snap['runs'] if x.get('ended_at')],
                     'wire_sha256':raw['wire_sha256'],'schema_sha256':raw['schema_sha256'],
                     'charged':True,'additional_calls':0})
    return rows


def execution_admission(adapter,wid,node):
    rows=execution_status(adapter,wid,node)
    blocked=[r for r in rows if r['state'] not in ('authenticated_result','reserved_not_dispatched','controlled_regeneration_authorized')]
    if blocked:
        fail('execution admission unmet: '+json.dumps(blocked,sort_keys=True)+
             '; need authenticated native result or certified pre-dispatch reservation; unknown remains charged, no resend')
    return rows

def reconcile(adapter,binding,record,reservation):
    """Recover audit persistence from a completed native session, never resend."""
    sid=record.get('session_id');run=record.get('native_run_id')
    if not sid or not run:return None
    home=native_home()
    if not (home/'state.db').is_file():return None
    with sqlite3.connect('file:'+str(home/'state.db')+'?mode=ro',uri=True) as db:
        if db.execute('SELECT source FROM sessions WHERE id=?',(sid,)).fetchone()!=('kanban',):return None
        rows=list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?',(sid,)))
    answers=[c for role,c,_ in rows if role=='assistant']
    if len(answers)!=1 or any(t and t!='[]' for _,_,t in rows):return None
    from fixed_semantic_recovery import _request_text,transcript_reply
    phase=json.loads(_request_text(record['wire']['user']))['phase']
    old=record['binding'];snap=adapter.kanban.task_snapshot(adapter.workflow.status(binding['workflow_id'])['kanban']['board_id'],old['task_id'])
    owner=json.loads(snap['task'].get('body','{}'))
    if (snap['task'].get('created_by')!='ingest-workflow' or owner.get('workflow_id')!=binding['workflow_id']
            or owner.get('node')!=old['node'] or owner.get('vault')!=str(adapter.workflow.vault)):
        fail('native card owner, node or Vault differs from its reserved provenance')
    if old['task_id']!=binding['task_id'] and (snap['task'].get('worker_pid') or not any(str(r['id'])==str(run) and r.get('ended_at') for r in snap['runs'])):
        fail('old native executor is not stopped for reconciliation')
    audit={'contract':'hermes-fixed-semantic-call/v1','workflow_id':binding['workflow_id'],
           'node':old['node'],'native_task_id':old['task_id'],'native_run_id':run,'template_hash':old['template_hash'],
           'phase':phase,'input_sha256':record['wire_sha256'],'user_sha256':fingerprint(record['wire']['user']),
           'schema_sha256':record['schema_sha256'],'response_sha256':fingerprint(answers[0]),
           'model':{'origin':'hermes_native_model','session_id':sid,'response_sha256':fingerprint(answers[0])},
           'image_inputs':record.get('images',[]),'receipt_refs':[],
           'reconciled_from_native_session':True,'usage_unknown':True}
    if phase=='citation':
        value=state(adapter.workflow.vault,binding['workflow_id']);frozen=plan(adapter.workflow.vault,value)
        request=json.loads(_request_text(record['wire']['user']))
        if len(request['input']['tasks'])!=1:return None
        audit['task_identities']={request['input']['tasks'][0]['task']:frozen['selection']['task_id']}
    if transcript_reply(audit,record['wire'],home=home) is None:return None
    ref=f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}/semantic-calls/reconciled-{fingerprint(audit)}.json'
    _write_atomic(_vault_path(adapter.workflow.vault,ref),_json_bytes(audit))
    update_call(reservation,audit_ref=ref,state='received',reconciliation={'prior_state':record['state'],'native_session_id':sid})
    return {**record,'audit_ref':ref,'state':'received'}

@contextmanager
def job(logical):
    token=JOB.set(logical)
    try:yield
    finally:JOB.reset(token)

def note_session(session):
    if CALL.get():update_call(CALL.get(),session_id=session)

def note_audit(ref):
    if CALL.get():update_call(CALL.get(),audit_ref=ref,state='received')

def proof(adapter,binding,record,*,require_stopped=False):
    record=execution_record(record)
    from fixed_semantic_recovery import native_proof
    path=_vault_path(adapter.workflow.vault,record['audit_ref'])
    old=record['binding']
    from citation_native_authority import isolated,worker_snapshot
    snap=(worker_snapshot(adapter,binding,record,require_stopped) if isolated() else
          adapter.kanban.task_snapshot(adapter.workflow.status(binding['workflow_id'])['kanban']['board_id'],old['task_id']))
    owner=json.loads(snap['task'].get('body','{}'))
    if (snap['task'].get('created_by')!='ingest-workflow' or owner.get('workflow_id')!=binding['workflow_id']
            or owner.get('node')!=old['node'] or owner.get('vault')!=str(adapter.workflow.vault)):
        fail('native card owner, node or Vault differs from its reserved provenance')
    current=old['task_id']==binding['task_id']
    runs=[str(r['id']) for r in snap['runs'] if r.get('ended_at')]
    if (require_stopped or not current) and (snap['task'].get('worker_pid') or not runs or any(not r.get('ended_at') for r in snap['runs'])):
        fail('old native executor must be stopped before journal reconciliation')
    audit=_load_json(path)
    images=audit.get('image_inputs',[])
    found=native_proof(adapter,binding,path,record['wire'],record['schema'],images,
        {old['task_id']:None if current else {'ended_run_ids':runs}})
    if found is None:fail('reservation has no authenticated native request/response')
    from fixed_semantic import decode_semantic_response
    body,_=decode_semantic_response(found[0])
    raw_body=copy.deepcopy(body)
    from fixed_semantic_recovery import _request_text
    request=json.loads(_request_text(record['wire']['user']))
    if audit['phase']!=request['phase']:fail('native phase differs from its actual request')
    if request['phase']=='citation':
        from fixed_semantic_schema import slot_results
        body=slot_results('citation',body,request['input']['tasks'])
    return {'audit_ref':record['audit_ref'],'audit':audit,'wire':record['wire'],'body':body,
            'raw_body':raw_body,'logical_binding':old}

def invoke(adapter,binding,phase,view,caller,limit,correction,callback,*,prepared=None):
    value=state(adapter.workflow.vault,binding['workflow_id'])
    if not value or value['phase']=='complete':return callback()
    frozen=plan(adapter.workflow.vault,value)
    if value['phase'] in ('citation','downstream'):
        live=binding
        workspace=os.environ.get('HERMES_KANBAN_WORKSPACE')
        if binding['node'].startswith('pass-slice:') and workspace and (Path(workspace)/'worker-request.json').exists():
            live=_load_json(Path(workspace)/'worker-request.json')
            if live['task_id']!=binding['task_id'] or live['node']!=binding['node']:fail('native request workspace changed owner')
        adapter.worker_check(live)  # Reject old executors before they can charge the ledger.
    logical=JOB.get()
    if phase=='citation' and value['phase']=='citation':
        if len(view.get('tasks',[]))!=1:fail('citation amendment owns one exact native task')
        logical='citation:'+frozen['selection']['task_id']
    elif value['phase'] not in ('downstream','awaiting_acceptance'):
        fail('only the selected native citation is executable in this amendment phase')
    if not logical or logical.partition(':')[0] not in frozen['budgets']:
        fail('fresh call is outside the explicit local amendment work; no batch-wide fallback')
    if correction:fail('amendment grants one reserved call, without automatic correction')
    from fixed_semantic import prepare_request_projection,HermesCaller
    from fixed_semantic_schema import slot_schema,schema_for
    from fixed_semantic_media import image_catalog,select_images
    schema=slot_schema(phase,view['tasks']) if phase=='citation' else schema_for(phase,view)
    images=[]
    if isinstance(caller,HermesCaller):
        original=view.get('original_evidence',view)
        catalog=image_catalog(adapter,original) if 'original_evidence' in view else getattr(caller,'image_catalog',{})
        images=select_images(original.get('tasks',[]),catalog)
    if prepared is None:
        prepared=prepare_request_projection(phase,view,schema,images,correction)
    elif prepared['identity']!={'phase':phase,'view':view,'output_schema':schema,'correction':correction,'images':images}:
        fail('final request projection differs from reservation input')
    if prepared['input_size']>limit:fail('complete local request exceeds its bound')
    expected={'citation':'citation','resource-reduce':'source','global-detail':'plan','page-write':'body'}
    if expected.get(phase)!=logical.partition(':')[0]:fail('logical budget does not own this native phase')
    reservation=reserve(adapter.workflow.vault,binding['workflow_id'],logical,prepared['wire'],schema,binding,
                        [{k:v for k,v in image.items() if k!='url'} for image in images])
    _,ledger,key,index,replayed=reservation;record=ledger['jobs'][key]['attempts'][index]
    if replayed:
        if not record.get('audit_ref'):
            if record.get('continuation') and record['state']=='reserved':
                pass  # The same charged ordinal; check_dispatch claims it atomically.
            else:
                record=reconcile(adapter,binding,execution_record(record),reservation)
                if not record:fail('reserved/unknown native completion remains charged; do not resend')
        if record.get('audit_ref'):
            found=proof(adapter,binding,record)
            from fixed_semantic_schema import slot_results,validate
            if phase=='citation':return slot_results(phase,found['raw_body'],view['tasks']),found['audit_ref']
            validate(phase,found['body'])
            return found['body'],found['audit_ref']
    token=CALL.set(reservation)
    try:
        result=callback();update_call(reservation,audit_ref=result[1],state='received')
        # Prove a genuine native response before a successor/domain commit.
        record=pending_calls(adapter.workflow.vault,binding['workflow_id'])['jobs'][key]['attempts'][index]
        proof(adapter,binding,record)
        return result
    finally:CALL.reset(token)

def authenticate(adapter,wid,phase,*,required_refs=None):
    value=state(adapter.workflow.vault,wid);found=[]
    for key,job_record in pending_calls(adapter.workflow.vault,wid)['jobs'].items():
        records=[r for r in job_record['attempts'] if r['operation_id']==value['operation_id'] and r['phase']==phase]
        if required_refs is not None and (not records or records[-1].get('audit_ref') not in required_refs):continue
        for record in records[:-1]:
            if not any(g['supersedes_attempt_sha256']==fingerprint(record) for g in value.get('retry_grants',{}).get(key,[])):
                fail('an older logical call has no explicit supersession; do not discard it')
        for record in records[-1:]:
            if record['operation_id']==value['operation_id'] and record['phase']==phase:
                if not record.get('audit_ref'):fail('unknown/reserved native call cannot be treated as uncalled')
                found.append(proof(adapter,record['binding'],record,require_stopped=True))
    return found

def verification_adapter(vault):
    from ingest_kanban import IngestKanbanAdapter
    return IngestKanbanAdapter(vault,enable_workers=True)

def verify_pending_submission(vault,wid,body):
    from hermes_source_units.workflow_guard import _BINDING
    binding=_BINDING.get()
    if not binding or binding['workflow_id']!=wid:fail('citation submission lacks the current native binding')
    value=state(vault,wid);frozen=plan(vault,value);records=[]
    for stored in pending_calls(vault,wid)['jobs'].values():
        records.extend(r for r in stored['attempts'] if r['operation_id']==value['operation_id']
                       and r['phase']=='citation')
    if not records or not records[-1].get('audit_ref'):fail('successor cannot commit without its reserved native response')
    adapter=verification_adapter(vault)
    call=proof(adapter,binding,records[-1])
    verify_citation_body(adapter,frozen,body,call)

def verify_citation_body(adapter,frozen,successor,call):
    from fixed_semantic_recovery import _request_text
    import json
    view=json.loads(_request_text(call['wire']['user']))['input']
    reviews=call['body'].get('reviews',[])
    if len(reviews)!=1 or reviews[0].get('decision')!='revised':fail('native successor needs one authored citation review')
    review=reviews[0];k=adapter.workflow.knowledge;tid=frozen['selection']['task_id']
    if call['audit'].get('task_identities',{}).get(review['task'])!=tid:fail('native citation task binding differs')
    old=_load_json(_vault_path(k.vault,frozen['old_pass_ref']))
    package=_load_json(_vault_path(k.vault,f'_system/knowledge-builds/task-{tid}/readings/{old["reading_package_id"]}.json'))
    mapping=k.model_input(tid,package,persist=False)['mapping']
    short=lambda c:{**copy.deepcopy(c),'support_refs':[k._model_handle(r,mapping) for r in c['support_refs']]}
    candidates={c['candidate_id']:short(c) for c in old['candidates']}
    if review.get('removed_candidate_ids'):fail('bounded citation revision cannot remove candidates')
    candidates.update({c['candidate_id']:c for c in review['candidates']})
    if candidates!={c['candidate_id']:short(c) for c in successor['candidates']}:
        fail('successor differs from the actual native candidate response')
    inspections={fingerprint(k._model_handle(i['source_ref'],mapping)):
                 {**i,'source_ref':k._model_handle(i['source_ref'],mapping)} for i in old['inspections']}
    inspections.update({fingerprint(i['source_ref']):i for i in review['inspections']})
    actual={fingerprint(k._model_handle(i['source_ref'],mapping)):
            {**i,'source_ref':k._model_handle(i['source_ref'],mapping)} for i in successor['inspections']}
    if inspections!=actual:fail('successor inspections differ from the native authored response')
    observed=view['tasks'][0].get('candidate',{})
    if observed.get('sequence')!=old['sequence']:fail('native review did not observe the pinned predecessor')
    if observed.get('candidates')!=[short(c) for c in old['candidates']]:fail('native review predecessor content differs')
    from revision_disposition import evidence
    projected=k.model_packet([k.model_input(tid,package,persist=False)],frozen['batch_id'],persist=False)['view']
    if evidence(view,view['tasks'][0])!=evidence(projected,projected['tasks'][0]):
        fail('native citation did not read the authorized current originals')
