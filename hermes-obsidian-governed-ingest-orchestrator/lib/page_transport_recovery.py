"""Authenticate stopped transient page calls; retry the same reservation only."""
import hashlib,sqlite3
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path,_json_bytes,_write_atomic,_exclusive_lock
from hermes_source_units.validation import fingerprint
from page_budget_store import INDEX,Snapshot,operation

CONTRACT='durable-page-transport-recovery/v1'
def fail(message):raise ContractError('RECOVERY_NOT_ALLOWED','$',message)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def identity(v):return fingerprint({k:v[k] for k in ('contract','view','backend')})
def transient(row):
    e=row.get('error',{});r=row.get('response_evidence',{})
    return (row.get('state')=='completion_unknown' and row.get('completed_output') is False
        and not (r.get('output') or r.get('choices') or r.get('partial'))
        and (e.get('class')=='APIConnectionError' or e.get('status_code') in (502,503,504)))
def contract_hash():
    p=Path(__file__).parent
    return fingerprint({n:sha(p/n) for n in ('page_transport_recovery.py','network_connection_retry.py',
        'native_page_pairs.py','split_page_calls.py','reduce_execution_recovery.py')})

def select(adapter,w):
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}'
    found=[]
    if not (root/'page-call-budget.json').exists():return found
    with operation():
        book=Snapshot(root)
        if book.index['contract']!=INDEX:return found
        for card in w['kanban']['task_map']:
            if card['node']!='global-reduce':continue
            fp=root/f'reports/failed-{fingerprint(card["node"])[:16]}.json'
            if not fp.exists():continue
            failure=_load_json(fp)
            if failure.get('task_id')!=card['task_id'] or failure.get('error_code')!='NATIVE_COMPLETION_UNKNOWN':continue
            failures=[p for p in (root/'call-failures').glob('*.json')
                if (r:=_load_json(p)).get('task_id')==card['task_id'] and r.get('phase')=='page-pair' and transient(r)]
            if not failures:continue
            binding=_load_json(root/f'bindings/{fingerprint(card["idempotency_key"])}.json')
            snapshots=[p for p in (root/'semantic-inputs').glob('*.json')
                if (s:=_load_json(p)).get('contract')=='fixed-reduce-input/v1' and s.get('binding')==binding]
            if len(snapshots)!=1:fail('transient page needs its unique original Reduce input')
            found.append({'node':'global-reduce','task_id':card['task_id'],'failure_sha256':sha(fp),
                'snapshot_ref':snapshots[0].relative_to(adapter.workflow.vault).as_posix(),
                'snapshot_sha256':sha(snapshots[0]),'page_transport_retry':True})
    return found

def verify(adapter,w,node,binding,entry,failure,runs):
    from fixed_reduce import preview_snapshot
    from fixed_semantic_recovery import native_proof,permitted_tasks,proof_scope
    from revision_disposition import native_user
    if node.kind!='global-reduce' or entry.get('page_transport_retry') is not True or failure.get('error_code')!='NATIVE_COMPLETION_UNKNOWN':
        fail('only ended transient page unknowns may continue')
    vault=adapter.workflow.vault;root=vault/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}'
    sp=_vault_path(vault,entry['snapshot_ref'])
    if sp.parent!=root/'semantic-inputs' or sha(sp)!=entry['snapshot_sha256']:fail('original input changed')
    value=_load_json(sp)
    if (value.get('binding')!=binding or identity(value)!=identity(preview_snapshot(adapter,binding))
            or value['input_id']!=fingerprint({k:v for k,v in value.items() if k!='input_id'})):
        fail('current complete source input differs')
    ended={str(r['id']) for r in runs}
    if not ended or any(not r.get('ended_at') for r in runs):fail('native producer must be ended')
    candidates=[]
    for p in (root/'call-failures').glob('*.json'):
        row=_load_json(p)
        if row.get('task_id')==binding['task_id'] and row.get('phase')=='page-pair' and transient(row):candidates.append((p,row))
    sealed=[]
    with operation(),proof_scope(),sqlite3.connect('file:'+str(Path.home()/'.hermes/state.db')+'?mode=ro',uri=True) as db:
        book=Snapshot(root)
        permitted=permitted_tasks(adapter,binding,compact=True)
        if book.index['contract']!=INDEX:fail('historical page ledger cannot be migrated for retry')
        for request in book.index['requests']:
            q=book.request(request)
            if 'response' in q:
                reply=q['response'];audit=_load_json(_vault_path(vault,reply['audit_ref']))
                proof=native_proof(adapter,binding,_vault_path(vault,reply['audit_ref']),q['wire'],q['schema'],q.get('image_inputs',[]),permitted)
                if (audit.get('page_request_id')!=request or not proof or proof[0]!=reply['raw']):
                    fail('saved response must authenticate before retaining pages')
                continue
            matches=[(p,row) for p,row in candidates if row.get('wire')==q['wire'] and row.get('schema_sha256')==fingerprint(q['schema'])]
            if len(matches)!=1:fail('unknown page has no unique transient failure; hold without resend')
            p,row=matches[0];sid=row['session_id']
            if (str(row.get('run_id')) not in ended or row.get('contract')!='native-call-outcome/v1'
                    or row.get('wire_sha256')!=fingerprint(q['wire']) or row.get('image_inputs',[])!=q.get('image_inputs',[])):
                fail('failure owner/request differs')
            if db.execute('SELECT source FROM sessions WHERE id=?',(sid,)).fetchone()!=('kanban',):fail('failed session is not native')
            messages=db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?',(sid,)).fetchall()
            if any(t and t!='[]' for _,_,t in messages) or any(role=='assistant' and (c or '').strip() for role,c,_ in messages):
                fail('possible returned content must be recovered before any resend')
            users=[native_user(c,{'user_sha256':fingerprint(q['wire']['user']),'image_inputs':q.get('image_inputs',[])})
                   for role,c,_ in messages if role=='user']
            if users.count(q['wire']['user'])!=1:fail('native request text differs')
            pages={}
            for m in q['members'].values():
                page=book.row(m['page_key'])
                if page.get('receipt') or not page['attempts'] or page['attempts'][-1] != {'ordinal':m['ordinal'],'request_id':request,'state':'reserved'}:
                    fail('valid, rejected or changed page cannot receive a transport resend')
                pages[m['page_key']]={'input_sha256':page['input_sha256'],'path':page['path'],'attempts_sha256':fingerprint(page['attempts'])}
            sealed.append({'request_id':request,'members':q['members'],'wire_sha256':fingerprint(q['wire']),
                'schema_sha256':fingerprint(q['schema']),'page_reservations':pages,
                'failure_ref':p.relative_to(vault).as_posix(),'failure_sha256':sha(p),'session_id':sid})
    if not sealed:fail('no unresolved transient page request')
    return {'page_transport_retry':{'contract':CONTRACT,'source_identity':identity(value),'requests':sealed,
            'new_semantic_ordinals':0,'original_unknowns_preserved':True}}

def permit(adapter,binding,value,request,q):
    from fixed_history import entries
    found=[]
    for pointer,entry in entries(adapter,binding,scopes=('reduce-cancel/v1',),reverse=True):
        proof=entry.get('page_transport_retry')
        if not proof:continue
        if proof.get('contract')!=CONTRACT or proof['source_identity']!=identity(value) or proof.get('new_semantic_ordinals')!=0:
            fail('transport recovery source/allowance changed')
        for row in proof['requests']:
            if row['request_id']!=request:continue
            if row['wire_sha256']!=fingerprint(q['wire']) or row['schema_sha256']!=fingerprint(q['schema']) or row['members']!=q['members']:
                fail('transport retry cannot change request or page combination')
            p=_vault_path(adapter.workflow.vault,row['failure_ref'])
            if sha(p)!=row['failure_sha256'] or not transient(_load_json(p)):fail('transient failure evidence changed')
            found.append({**row,'recovery_id':pointer['recovery_id'],'authorization_ref':pointer['report_ref']})
        if found:break
    if len(found)>1:fail('ambiguous transport grants')
    return found[0] if found else None

def recover(adapter,wid,*,resume=True):
    from automatic_pass_recovery import enabled,request
    from native_ingest import control_workflow
    from native_stop_inventory import verify_cancel_drain
    w=adapter.workflow.status(wid)
    if not enabled(w) or w['current_stage']!='reducing' or adapter.workflow.pending_pause(w):return False
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-page-transport'
    with _exclusive_lock(root/'.controller.lock'):
        pending=root/'pending.json'
        if pending.exists():intent=_load_json(pending)
        else:
            selected=select(adapter,w)
            if not selected:return False
            intent={'entries':selected,'operation_id':'auto-page-transport-'+fingerprint(selected)[:20]}
            _write_atomic(pending,_json_bytes(intent))
        op=intent['operation_id']
        if not w['cancel_requested']:control_workflow(adapter.workflow.vault,wid,'cancel',op+'-stop')
        verify_cancel_drain(adapter,wid,timeout=0)
        rp=root/(op+'.request.json')
        if not rp.exists():_write_atomic(rp,_json_bytes(request(adapter,wid,recovery_id=op,recovery_scope='reduce-cancel/v1',
            target_contract_sha256=contract_hash(),reason='Continue exact transient page transport reservation; retain all valid results and unknown charges',entries=intent['entries'])))
        result=adapter.recover_reduce_execution(_load_json(rp))
        _write_atomic(root/(op+'.json'),_json_bytes({**intent,'state':'applied','recovery_result':result,'new_semantic_ordinals':0}))
        if resume:control_workflow(adapter.workflow.vault,wid,'resume',op+'-resume')
        pending.unlink();return True
