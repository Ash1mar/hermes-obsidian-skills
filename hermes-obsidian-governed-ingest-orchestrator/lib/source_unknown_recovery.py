"""Authenticate and quarantine exact stopped source packets, without another call.

Uses existing reduce-cancel admission and native worker coverage omissions. Valid
packets replay normally; unknown journals and original task budgets stay sealed.
"""
import hashlib,json,sqlite3
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes,_exclusive_lock

CONTRACT='source-unknown-isolation/v1'

def fail(message):raise ContractError('RECOVERY_NOT_ALLOWED','$',message)
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def identity(value):return fingerprint({k:value[k] for k in ('contract','view','backend')})

def enabled(workflow):
    from hermes_source_units.recovery_policy import effective
    from orchestration import configuration
    return (workflow.get('scope',{}).get('execution_mode')=='auto_full'
        and effective(workflow,configuration())['unknown_completion']=='isolate_after_drain')

def contract_hash():
    root=Path(__file__).parent
    return fingerprint({n:sha(root/n) for n in ('source_unknown_recovery.py','native_call_state.py',
        'fixed_reduce_packets.py','fixed_semantic.py','reduce_execution_recovery.py')})

def verify(adapter,workflow,node,binding,entry,failure,runs,proof):
    """Called inside ordinary stopped native recovery admission, never by a worker."""
    from fixed_reduce import preview_snapshot
    from fixed_reduce_packets import plan
    from fixed_semantic import prepare_request_projection
    from source_result_review import review_view
    from semantic_output import path_for
    if (node.kind!='resource-reduce' or failure.get('error_code')!='NATIVE_COMPLETION_UNKNOWN'
            or not enabled(workflow) or entry.get('source_unknown_isolation') is not True):
        fail('only policy-authorized stopped source unknown packets may be isolated')
    path=_vault_path(adapter.workflow.vault,entry['snapshot_ref'])
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}"
    if path.parent!=root/'semantic-inputs' or sha(path)!=entry['snapshot_sha256']:fail('source snapshot changed')
    old=_load_json(path);current=preview_snapshot(adapter,binding)
    if old['binding']!=binding or identity(old)!=identity(current):fail('current complete source input changed')
    if old['input_id']!=fingerprint({k:v for k,v in old.items() if k!='input_id'}):fail('source snapshot identity differs')
    offered={}
    for packet in plan(adapter,old,prepare_request_projection):
        view=review_view(old,packet['view']);jp=path_for(adapter,binding,'resource-reduce',view)
        offered[jp.name]=(packet,view)
    outcomes=proof.get('native_call_outcomes',[])
    if not outcomes or not proof.get('reserved_attempts_preserved') or proof.get('new_calls_granted')!=0:
        fail('unknown isolation requires ordinary native call authentication')
    sealed=[]
    with sqlite3.connect('file:'+str(Path.home()/'.hermes/state.db')+'?mode=ro',uri=True) as db:
        for outcome in outcomes:
            if outcome['state']!='completion_unknown' or outcome.get('legacy_native_dump'):fail('non-unknown call remains held')
            name=outcome['journal_name']
            if name not in offered:fail('unknown call is not a current source packet')
            packet,view=offered[name];jp=root/'output-attempts'/name;journal=_load_json(jp)
            if not packet['candidate_refs']:fail('empty packet unknown requires explicit review')
            rp=Path(outcome['evidence_path']);report=_load_json(rp);last=journal['attempts'][-1]
            if (sha(jp)!=outcome['journal_sha256'] or sha(rp)!=outcome['evidence_sha256']
                    or rp.parent!=root/'call-failures' or report.get('phase')!='resource-reduce'
                    or report.get('view')!=view or journal['input_sha256']!=fingerprint(view)
                    or last.get('attempt_id')!=outcome['attempt_id'] or last.get('state') not in ('reserved','rejected')
                    or last.get('audit_ref') or last.get('session_id')!=outcome['session_id']):
                fail('unknown reservation or exact current packet differs')
            response=report.get('response_evidence',{})
            if response.get('output') or response.get('choices'):fail('returned response must be authenticated first')
            if db.execute("SELECT 1 FROM messages WHERE session_id=? AND role='assistant' AND trim(coalesce(content,''))<>'' LIMIT 1",(outcome['session_id'],)).fetchone():
                fail('possible returned answer must be authenticated before isolation')
            sealed.append({'journal_name':name,'journal_sha256':sha(jp),'input_sha256':fingerprint(view),
                'candidate_refs':packet['candidate_refs'],'call_failure_ref':rp.relative_to(adapter.workflow.vault).as_posix(),
                'call_failure_sha256':sha(rp),'session_id':outcome['session_id'],'reserved_ordinal':outcome['ordinal']})
    if len({r['journal_name'] for r in sealed})!=len(sealed):fail('multiple unknown owners for one packet')
    return {'source_unknown_isolation':{'contract':CONTRACT,'source_identity':identity(old),
        'snapshot_ref':entry['snapshot_ref'],'snapshot_sha256':entry['snapshot_sha256'],'packets':sealed,
        'new_model_calls_granted':0,'unknowns_preserved':True,'coverage_complete':False}}

def packet_disposition(adapter,binding,value,packet):
    """Freshly validate sealed omission authority before skipping the old journal."""
    from fixed_history import entries
    from source_result_review import review_view
    from semantic_output import path_for
    view=review_view(value,packet['view']);name=path_for(adapter,binding,'resource-reduce',view).name
    found=[]
    for pointer,entry in entries(adapter,binding,scopes=('reduce-cancel/v1',)):
        proof=entry.get('source_unknown_isolation')
        if not proof:continue
        if proof.get('contract')!=CONTRACT or proof.get('new_model_calls_granted')!=0 or not proof.get('unknowns_preserved'):
            fail('source isolation authority differs')
        if proof['source_identity']!=identity(value):fail('isolated source inputs changed')
        sp=_vault_path(adapter.workflow.vault,proof['snapshot_ref'])
        if sha(sp)!=proof['snapshot_sha256']:fail('isolated original snapshot changed')
        for row in proof['packets']:
            if row['journal_name']!=name:continue
            jp=path_for(adapter,binding,'resource-reduce',view);rp=_vault_path(adapter.workflow.vault,row['call_failure_ref'])
            if (sha(jp)!=row['journal_sha256'] or sha(rp)!=row['call_failure_sha256']
                    or row['input_sha256']!=fingerprint(view) or row['candidate_refs']!=packet['candidate_refs']):
                fail('isolated packet, original budget or evidence changed')
            found.append({**row,'authorization_ref':pointer['report_ref']})
    if len(found)>1:fail('multiple isolation grants for the same unknown packet')
    return found[0] if found else None

def select(adapter,workflow):
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}"
    selected=[]
    for card in workflow['kanban']['task_map']:
        if not card['node'].startswith('resource-reduce:'):continue
        fp=root/f"reports/failed-{fingerprint(card['node'])[:16]}.json"
        if not fp.exists():continue
        failure=_load_json(fp)
        if failure.get('task_id')!=card['task_id'] or failure.get('error_code')!='NATIVE_COMPLETION_UNKNOWN':continue
        binding=_load_json(root/f"bindings/{fingerprint(card['idempotency_key'])}.json")
        snapshots=[]
        for p in (root/'semantic-inputs').glob('*.json'):
            row=_load_json(p)
            if row.get('binding')==binding and 'backend' in row:snapshots.append(p)
        if len(snapshots)!=1:fail('unknown source requires its unique immutable input snapshot')
        sp=snapshots[0]
        selected.append({'node':card['node'],'task_id':card['task_id'],'failure_sha256':sha(fp),
            'snapshot_ref':sp.relative_to(adapter.workflow.vault).as_posix(),'snapshot_sha256':sha(sp),'source_unknown_isolation':True})
    return selected

def admitted_intent(adapter,workflow,intent):
    """Settle an old intent after exact supported admission under corrected code.

    The original saved request remains immutable. Only hash-verified recovery
    reports for every original task/failure/snapshot can replace its missing
    receipt; a changed contract alone never grants execution or fresh budget.
    """
    from fixed_history import checked_report
    matches=[]
    for selected in intent['entries']:
        found=[]
        for pointer in workflow.get('execution_recovery_history',[]):
            report=checked_report(adapter.workflow,pointer)
            if report['request'].get('recovery_scope')!='reduce-cancel/v1':continue
            for entry in report['entries']:
                proof=entry.get('source_unknown_isolation',{})
                if (entry.get('node')==selected['node'] and entry.get('previous_task_id')==selected['task_id']
                        and entry.get('failure_sha256')==selected['failure_sha256']
                        and proof.get('contract')==CONTRACT and proof.get('snapshot_ref')==selected['snapshot_ref']
                        and proof.get('snapshot_sha256')==selected['snapshot_sha256']
                        and proof.get('new_model_calls_granted')==0 and proof.get('unknowns_preserved') is True):
                    found.append(pointer['report_ref'])
        if not found:return None
        if len(found)!=1:fail('multiple admissions for one original unknown intent')
        matches.extend(found)
    return {'ok':True,'applied':False,'reused_admission_refs':matches,'new_model_calls_granted':0}

def recover(adapter,wid,*,resume=True):
    from automatic_pass_recovery import request
    from native_ingest import control_workflow
    from native_stop_inventory import verify_cancel_drain
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-source-unknown'
    with _exclusive_lock(root/'.controller.lock'):
        w=adapter.workflow.status(wid)
        if not enabled(w) or w['current_stage']!='reducing' or adapter.workflow.pending_pause(w):return False
        pending=root/'pending.json'
        if pending.exists():intent=_load_json(pending)
        else:
            if w['cancel_requested']:return False
            selected=select(adapter,w)
            if not selected:return False
            intent={'contract':CONTRACT,'operation_id':'auto-source-unknown-'+fingerprint(selected)[:24],'entries':selected}
            _write_atomic(pending,_json_bytes(intent))
        if intent['contract']!=CONTRACT:fail('unknown isolation intent changed')
        op=intent['operation_id']
        if not w['cancel_requested']:control_workflow(adapter.workflow.vault,wid,'cancel',op+'-stop')
        verify_cancel_drain(adapter,wid,timeout=0)
        result=admitted_intent(adapter,adapter.workflow.status(wid),intent)
        if result is None:
            rp=root/(op+'.request.json')
            if not rp.exists():_write_atomic(rp,_json_bytes(request(adapter,wid,recovery_id=op,
                recovery_scope='reduce-cancel/v1',target_contract_sha256=contract_hash(),
                reason='Quarantine exact stopped unknown source packets; preserve reservations and all valid results; zero retry grants',entries=intent['entries'])))
            result=adapter.recover_reduce_execution(_load_json(rp))
        _write_atomic(root/(op+'.json'),_json_bytes({**intent,'state':'applied','result':result,'new_model_calls_granted':0}))
        if resume:control_workflow(adapter.workflow.vault,wid,'resume',op+'-resume')
        pending.unlink();return True
