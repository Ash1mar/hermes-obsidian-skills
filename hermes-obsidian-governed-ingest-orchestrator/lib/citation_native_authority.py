"""Trusted native ownership seals; workers cannot run the Kanban CLI."""
import json
import os
from pathlib import Path
from hermes_source_units.citation_amendment import state,root,fail,pending_calls
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes,_exclusive_lock
from hermes_source_units.validation import fingerprint

def isolated():
    return bool(os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'))

def descriptor(vault,wid,value):
    return root(vault,wid)/value['operation_id']/'native-authority/index.json'

def read_seal(vault,index,kind,key):
    import hashlib
    row=index.get(kind,{}).get(key)
    if not row:fail('trusted native ownership evidence is not sealed; wait for the orchestrator')
    path=_vault_path(vault,row['ref'])
    if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:fail('trusted native ownership seal changed')
    return _load_json(path)

def write_seal(vault,index,kind,key,value,path):
    import hashlib
    raw=_json_bytes(value);target=path.parent/(kind+'-'+fingerprint(value)+'.json')
    if target.exists() and target.read_bytes()!=raw:fail('native evidence identity collision')
    if not target.exists():_write_atomic(target,raw)
    index.setdefault(kind,{})[key]={'ref':target.relative_to(vault).as_posix(),'sha256':hashlib.sha256(raw).hexdigest()}

def validate_owner(adapter,w,card,binding,snapshot):
    task=snapshot['task'];body=json.loads(task.get('body','{}'))
    expected={'workflow_id':w['workflow_id'],'node':card['node'],'vault':str(adapter.workflow.vault),
              'input_fingerprint':binding['input_fingerprint'],'executor':'semantic-v1'}
    expected['worker_binding']=str(_vault_path(adapter.workflow.vault,
        f'_system/ledgers/ingest-workflows/{w["workflow_id"]}/bindings/{fingerprint(card["idempotency_key"])}.json'))
    expected['slice_template_hash' if card['node'].startswith('pass-slice:') else 'template_hash']=binding['template_hash']
    if (task.get('id')!=card['task_id'] or task.get('created_by')!='ingest-workflow'
            or not card['idempotency_key'].endswith(':'+binding['input_fingerprint'])
            or any(body.get(k)!=v for k,v in expected.items())):
        fail('native owner/body differs from the current canonical amendment card')

def capture(adapter,wid):
    """Called by trusted dispatch before claims; never by a domain worker."""
    if isolated():fail('only the trusted orchestrator may seal native ownership')
    vault=adapter.workflow.vault;value=state(vault,wid)
    if not value or value['phase']=='complete':return
    w=adapter.workflow.status(wid);path=descriptor(vault,wid,value)
    with _exclusive_lock(path.with_suffix('.lock')):
        index=_load_json(path) if path.exists() else {'operation_id':value['operation_id'],'plan_sha256':value['plan_sha256']}
        nodes=set(value.get('downstream_nodes',[]))
        if value['phase']=='citation':nodes.update('pass-slice:'+s for s in w.get('pass_revision',{}).get('slice_ids',[]))
        for card in w['kanban']['task_map']:
            if card['node'] not in nodes:continue
            binding=_load_json(_vault_path(vault,f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(card["idempotency_key"])}.json'))
            key=fingerprint(binding)
            if key in index.get('cards',{}):continue
            snap=adapter.kanban.task_snapshot(w['kanban']['board_id'],card['task_id'])
            validate_owner(adapter,w,card,binding,snap)
            write_seal(vault,index,'cards',key,{'binding':binding,'task':snap['task'],
                       'board_id':w['kanban']['board_id'],'operation_id':value['operation_id']},path)
        from citation_amendment_calls import proof
        for logical,job in pending_calls(vault,wid).get('jobs',{}).items():
            for ordinal,record in enumerate(job['attempts']):
                key=fingerprint(record)
                if record['operation_id']!=value['operation_id'] or key in index.get('ended_calls',{}):continue
                old=record['binding'];snap=adapter.kanban.task_snapshot(w['kanban']['board_id'],old['task_id'])
                if (snap['task'].get('worker_pid') or snap['task'].get('current_run_id') or not snap.get('runs')
                        or any(not r.get('ended_at') for r in snap['runs'])):continue
                if not record.get('audit_ref'):
                    from citation_amendment_calls import reconcile
                    reservation=(root(vault,wid)/'budgets.json',None,logical,ordinal,True)
                    if not reconcile(adapter,old,record,reservation):continue
                    record=pending_calls(vault,wid)['jobs'][logical]['attempts'][ordinal];key=fingerprint(record)
                proof(adapter,old,record,require_stopped=True)
                if str(record.get('native_run_id')) not in {str(r['id']) for r in snap['runs']}:fail('native reservation run is absent')
                write_seal(vault,index,'ended_calls',key,{'record_sha256':key,'snapshot':snap,
                    'operation_id':value['operation_id'],'board_id':w['kanban']['board_id']},path)
        _write_atomic(path,_json_bytes(index))

def worker_snapshot(adapter,binding,record,require_stopped):
    """Read only sealed metadata plus current canonical worker authority."""
    vault=adapter.workflow.vault;wid=binding['workflow_id'];value=state(vault,wid)
    if not value or record['operation_id']!=value['operation_id']:fail('native reservation belongs to another amendment')
    w=adapter.workflow.status(wid);task_id=os.environ.get('HERMES_KANBAN_TASK')
    if (not task_id or os.environ.get('HERMES_KANBAN_BOARD')!=w['kanban']['board_id']
            or os.environ.get('HERMES_INGEST_EXECUTOR')!='semantic-v1' or not os.environ.get('HERMES_KANBAN_RUN_ID')):
        fail('native proof requires the actual current semantic executor')
    cards=[c for c in w['kanban']['task_map'] if c['task_id']==task_id]
    if len(cards)!=1:fail('native proof executor has no current canonical card')
    card=cards[0];artifact=_vault_path(vault,f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(card["idempotency_key"])}.json')
    from program_worker import canonical_binding
    current,_=canonical_binding(adapter,artifact,executor='semantic-v1')
    live=current;workspace=os.environ.get('HERMES_KANBAN_WORKSPACE')
    if card['node'].startswith('pass-slice:') and workspace:
        live=_load_json(Path(workspace)/'worker-request.json')
        if any(live.get(k)!=v for k,v in current.items()):fail('current worker workspace differs from its canonical owner')
    adapter.worker_check(live)
    index=_load_json(descriptor(vault,wid,value))
    if index['operation_id']!=value['operation_id'] or index['plan_sha256']!=value['plan_sha256']:fail('native authority belongs to another plan')
    owned=read_seal(vault,index,'cards',fingerprint(current))
    if owned['binding']!=current or owned['board_id']!=w['kanban']['board_id']:fail('sealed current owner differs')
    if record['binding']['task_id']==task_id and not require_stopped:
        if (any(record['binding'].get(k)!=v for k,v in current.items())
                or str(record.get('native_run_id'))!=os.environ['HERMES_KANBAN_RUN_ID']):
            fail('native reservation differs from the actual current run')
        return {'task':owned['task'],'runs':[]}
    ended=read_seal(vault,index,'ended_calls',fingerprint(record))
    if ended['record_sha256']!=fingerprint(record) or ended['board_id']!=w['kanban']['board_id']:fail('ended native evidence differs')
    return ended['snapshot']

def verify_replay_recovery(adapter,workflow,request,binding):
    """Zero-call rebinding after a program repair; never retry a denied request."""
    if isolated():fail('native-result replay admission requires the trusted operator')
    vault=adapter.workflow.vault;wid=workflow['workflow_id'];value=state(vault,wid)
    if (not value or value['phase']!='citation' or request.get('amendment_id')!=value['operation_id']
            or workflow.get('pass_revision',{}).get('revision_id')!=value['revision_id']):
        fail('native replay recovery is outside the current selected citation amendment')
    from citation_amendment_control import contract_hash
    if request.get('target_contract_sha256')!=contract_hash():fail('corrected native replay contract differs')
    import hashlib
    basis=request.get('basis',[])
    if not basis:fail('native replay recovery needs pinned corrected program evidence')
    for item in basis:
        if hashlib.sha256(_vault_path(vault,item['ref']).read_bytes()).hexdigest()!=item['sha256']:fail('corrected program cause changed')
    capture(adapter,wid)
    records=[r for job in pending_calls(vault,wid)['jobs'].values() for r in job['attempts']
             if r['operation_id']==value['operation_id'] and r['phase']=='citation' and r['binding']['task_id']==binding['task_id']]
    if len(records)!=1 or not records[0].get('audit_ref'):fail('replay recovery needs one complete authenticated native response')
    record=records[0]
    from citation_amendment_calls import proof,verify_citation_body
    found=proof(adapter,binding,record,require_stopped=True)
    reviews=found['body'].get('reviews',[])
    if len(reviews)!=1 or reviews[0].get('decision')!='revised':fail('a native refusal/unknown is not an adoptable replay result')
    from hermes_source_units.citation_amendment import plan
    frozen=plan(vault,value)
    from fixed_semantic_recovery import _request_text
    wire=json.loads(_request_text(found['wire']['user']))
    if (wire['phase']!='citation' or len(wire['input']['tasks'])!=1
            or found['audit'].get('task_identities',{}).get(reviews[0]['task'])!=frozen['selection']['task_id']):
        fail('authenticated replay does not own the exact selected logical citation')
    return {'audit_ref':record['audit_ref'],'audit_sha256':hashlib.sha256(_vault_path(vault,record['audit_ref']).read_bytes()).hexdigest(),
            'reservation_sha256':fingerprint(record),'wire_sha256':record['wire_sha256'],
            'schema_sha256':record['schema_sha256'],'native_run_id':record['native_run_id'],
            'new_model_calls_granted':0,'original_attempts_and_unknowns_preserved':True}
