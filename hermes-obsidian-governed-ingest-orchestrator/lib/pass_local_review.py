"""Program admission for one additional call, sealed to rejected native input."""
from pathlib import Path
import ast,hashlib,json,sqlite3,os
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes,_exclusive_lock

SCOPE='pass-local-review/v1'


def fail(message):raise ContractError('RECOVERY_NOT_ALLOWED','$',message)


def verify(adapter,w,item,binding,failure,snapshot,*,call_count=1):
    """Admission authenticates existing source/packet/native evidence; never edits output."""
    from fixed_semantic import COMMON,RULES,decode_semantic_response,visible_packet,subset
    from fixed_semantic_recovery import _request_text,task_content
    from hermes_source_units.pass_skips import skippable_failure
    category=skippable_failure(failure)
    if category not in ('UNVERIFIED_CONTENT_OR_INSPECTION','SEMANTIC_REVIEW_REQUIRED','SEMANTIC_DRAFT_REJECTED'):
        fail('only known returned content/format failures qualify')
    if not snapshot.get('runs') or any(not r.get('ended_at') for r in snapshot['runs']):fail('native attempt must have ended')
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}/semantic-calls'
    calls=[(p,_load_json(p)) for p in root.glob('*.json')]
    calls=[(p,c) for p,c in calls if c.get('native_task_id')==binding['task_id'] and c.get('phase') in ('candidate','citation') and not c.get('reused_from_audit')]
    rejected=[(p,c) for p,c in calls if c.get('validated') is False]
    if len(rejected)!=call_count:fail('requires exact rejected native calls, no unknown completion')
    path,call=max(rejected,key=lambda row:row[0].stat().st_mtime_ns);phase=call['phase']
    if len([c for _,c in calls if c['phase']==phase])!=call_count:fail('additional call already spent or multiple calls need explicit diagnosis')
    if call.get('semantic_attempt_limit')!=2 or call['model'].get('api_calls')!=1:fail('original finite one-call evidence is required')
    if str(call.get('native_run_id')) not in {str(r['id']) for r in snapshot['runs']}:fail('native run does not own the response')
    session=call['model']['session_id']
    with sqlite3.connect('file:'+str(Path.home()/'.hermes/state.db')+'?mode=ro',uri=True) as db:
        record=db.execute('select source from sessions where id=?',(session,)).fetchone()
        if record!=('kanban',):fail('response is not from a native worker')
        rows=list(db.execute('select role,content,tool_calls from messages where session_id=?',(session,)))
        for _,prior in rejected:
            if prior['model'].get('api_calls')!=1 or str(prior.get('native_run_id')) not in {str(r['id']) for r in snapshot['runs']}:
                fail('every consumed attempt must be a single ended native call')
            sid=prior['model']['session_id']
            if db.execute('select source from sessions where id=?',(sid,)).fetchone()!=('kanban',):fail('non-native prior attempt')
            history=list(db.execute('select role,content,tool_calls from messages where session_id=?',(sid,)))
            prompts=[c for r,c,_ in history if r=='user' and fingerprint(c)==prior['user_sha256']]
            if (len(prompts)!=1 or any(tc and tc!='[]' for _,_,tc in history)
                    or not any(r=='assistant' and fingerprint(c)==prior['response_sha256'] for r,c,_ in history)
                    or prior['input_sha256']!=fingerprint({'system':COMMON+'\n'+RULES[prior['phase']],'user':prompts[0]})):
                fail('prior request or returned response cannot be authenticated')
    if any(tools and tools!='[]' for _,_,tools in rows):fail('semantic session had tools')
    users=[c for r,c,_ in rows if r=='user' and fingerprint(c)==call['user_sha256']]
    replies=[c for r,c,_ in rows if r=='assistant' and fingerprint(c)==call['response_sha256']]
    if len(users)!=1 or len(replies)!=1:fail('native transcript hash differs')
    if call['input_sha256']!=fingerprint({'system':COMMON+'\n'+RULES[phase],'user':users[0]}):fail('original system/user input cannot be authenticated')
    body,end=json.JSONDecoder().raw_decode(_request_text(users[0]));view=body['input']
    if body['phase']!=phase:fail('unsupported native result phase')
    pending=[];identities={};batch=adapter.workflow.knowledge._batch(w['batch_id'])
    for t in view['tasks']:
        tid=call['task_identities'][t['task']]
        if tid not in item['task_ids']:fail('native output names foreign work')
        task=adapter.workflow.knowledge._task(tid)
        if task['status']=='completed' or adapter.workflow._task_passes_complete(tid):continue
        projection=adapter.workflow.knowledge.preview_model_input(tid,batch)
        # Build an ordinary current packet so pools and heading aliases are compared by content.
        packet=visible_packet(adapter.workflow.knowledge.model_packet([projection],w['batch_id'],persist=False)['view'])
        if phase=='candidate' and packet['tasks'][0]['continuation']['action']=='citation':continue
        packet=subset(packet,packet['tasks'],phase)
        if task_content(view,t)!=task_content(packet,packet['tasks'][0]):fail('current reading/input differs from rejected call')
        pending.append(tid);identities[tid]=task_content(view,t)
    if not pending:fail('no pending work remains')
    return {'contract':SCOPE,'phase':phase,'task_ids':pending,'task_input_fingerprints':identities,
        'original_call_ref':path.relative_to(adapter.workflow.vault).as_posix(),'original_call_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'session_id':session,'response_sha256':call['response_sha256'],'additional_calls':1,
        'original_attempts_preserved':True,'unknown_completion_allowed':False}


def exhaustion_proof(adapter,w,item,binding,failure,snapshot):
    """Prove two returned attempts; a reserved or unknown call never qualifies."""
    prior=grants(adapter,binding)
    if prior:
        if len(prior)!=1:fail('ambiguous correction authority')
        grant=prior[0]
        path=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}/local-review-calls/{grant["recovery_id"]}-{fingerprint(binding["node"])[:16]}.json'
        if not path.exists():fail('correction not yet dispatched')
        reservation=_load_json(path)
        if reservation['state']!='returned' or reservation['native_task_id']!=binding['task_id']:
            fail('unknown correction completion remains held')
        proof=verify(adapter,w,item,binding,failure,snapshot)
        if proof['original_call_ref']!=reservation['audit_ref']:fail('correction differs from reservation')
        proof.update(attempt_count=2,prior_call_ref=grant['original_call_ref'],
            prior_call_sha256=grant['original_call_sha256'],reservation_ref=path.relative_to(adapter.workflow.vault).as_posix(),
            reservation_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return proof
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}/semantic-calls'
    rejected=[_load_json(p) for p in root.glob('*.json') if _load_json(p).get('native_task_id')==binding['task_id']
        and _load_json(p).get('validated') is False and not _load_json(p).get('reused_from_audit')]
    if len(rejected)<2:return None
    proof=verify(adapter,w,item,binding,failure,snapshot,call_count=2)
    proof['attempt_count']=2
    return proof


def grants(adapter,binding):
    w=adapter.workflow.status(binding['workflow_id']);out=[]
    for pointer in w.get('execution_recovery_history',[]):
        path=_vault_path(adapter.workflow.vault,pointer['report_ref']);data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=pointer['report_sha256']:fail('recovery authority changed')
        report=json.loads(data)
        if report['request'].get('recovery_scope')!=SCOPE:continue
        for entry in report['entries']:
            if entry['node']==binding['node']:
                proof=entry['local_review'];p=_vault_path(adapter.workflow.vault,proof['original_call_ref'])
                if hashlib.sha256(p.read_bytes()).hexdigest()!=proof['original_call_sha256']:fail('original rejected call changed')
                out.append({**proof,'recovery_id':pointer['recovery_id'],'recovery_ref':pointer['report_ref']})
    return out


def reserve(adapter,binding,grant,phase,view,tasks,ids):
    from fixed_semantic_recovery import task_content
    real=[ids[t['task']] for t in tasks]
    if set(real)!=set(grant['task_ids']):fail('local review must use its exact pending scope')
    for t,tid in zip(tasks,real):
        exposed=next(x for x in view['tasks'] if x['task']==t['task'])
        if task_content(view,exposed)!=grant['task_input_fingerprints'][tid]:fail('review material changed')
    ref=f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}/local-review-calls/{grant["recovery_id"]}-{fingerprint(binding["node"])[:16]}.json'
    path=_vault_path(adapter.workflow.vault,ref)
    with _exclusive_lock(path.with_suffix('.lock'), timeout=5):
        if path.exists():fail('the one additional request is already reserved; authenticate completion or isolate, never resend')
        record={'contract':SCOPE,'state':'reserved','phase':phase,'task_ids':real,'native_task_id':binding['task_id'],
            'recovery_ref':grant['recovery_ref'],'original_call_ref':grant['original_call_ref'],
            'input_fingerprint':fingerprint(view),'attempt_ordinal':2,'additional_call_count':1}
        _write_atomic(path,_json_bytes(record))
    return ref


def returned(adapter,ref,audit_ref):
    path=_vault_path(adapter.workflow.vault,ref);record=_load_json(path)
    record.update(state='returned',audit_ref=audit_ref);_write_atomic(path,_json_bytes(record))


def reserve_budget(adapter,binding,phase,view,tasks,ids,grant=None,limit=2):
    """Attempt allowance belongs to material/task, never worker, group or restart."""
    from automatic_pass_recovery import enabled
    from fixed_semantic_recovery import task_content
    if not enabled(adapter.workflow.status(binding['workflow_id'])):return None
    path=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}/pass-call-budget.json'
    with _exclusive_lock(path.with_suffix('.lock'), timeout=5):
        ledger=_load_json(path) if path.exists() else {'contract':'durable-pass-call-budget/v1','tasks':{}}
        keys=[]
        for t in tasks:
            tid=ids[t['task']];key=fingerprint({'node':binding['node'],'task':tid,'phase':phase,'input':task_content(view,t)})
            row=ledger['tasks'].setdefault(key,{'task_id':tid,'phase':phase,'attempts':[]})
            if grant and not row['attempts']:
                row['attempts'].append({'ordinal':1,'state':'returned','audit_ref':grant['original_call_ref'],'adopted_native_evidence':True})
            if row['attempts'] and row['attempts'][-1]['state']=='reserved':fail('native completion unknown; no further call')
            if len(row['attempts'])>=limit:fail('durable Pass call allowance exhausted')
            keys.append(key)
        for key in keys:
            attempts=ledger['tasks'][key]['attempts']
            attempts.append({'ordinal':len(attempts)+1,'state':'reserved','native_task_id':binding['task_id']})
        _write_atomic(path,_json_bytes(ledger))
    return path,keys


def finish_budget(reservation,audit_ref):
    if not reservation:return
    path,keys=reservation
    with _exclusive_lock(path.with_suffix('.lock'), timeout=5):
        ledger=_load_json(path)
        for key in keys:ledger['tasks'][key]['attempts'][-1].update(state='returned',audit_ref=audit_ref)
        _write_atomic(path,_json_bytes(ledger))
