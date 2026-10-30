"""Deterministic operator flow: bounded native repair, exclude, native close, pause.

No LLM chooses repair candidates, budgets, categories, dependencies or exclusions.
Two native cycles maximum. Neither cycle releases a durable boundary.
"""
import hashlib,json,subprocess,sys,time
from pathlib import Path
from hermes_source_units import mutation_digest,ContractError
from hermes_source_units.source_units import _load_json,_write_atomic,_json_bytes,_exclusive_lock,_vault_path
from hermes_source_units.validation import fingerprint,record_validation_scope
from hermes_source_units.finalize_revision_state import load
from hermes_source_units.partial_finalize import pin
from hermes_source_units.delivery_scope import batch_complete,closure_refs,fail
from native_ingest import control_workflow
from native_stop_inventory import workflow_processes,verify_cancel_drain

def inventory_for(adapter,w):
    from fixed_semantic_recovery import permitted_tasks
    card=next(c for c in w['kanban']['task_map'] if c['node']=='build-finalize')
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}"
    binding=_load_json(root/f"bindings/{fingerprint(card['idempotency_key'])}.json")
    owners=list(permitted_tasks(adapter,binding,compact=True))
    order=[card['task_id'],*reversed([o for o in owners if o!=card['task_id']])]
    found=[(p,_load_json(p)) for p in (root/'build-dispositions').glob('*.json')]
    for owner in order:
        rows=[(p,v) for p,v in found if v['binding']['task_id']==owner]
        if len(rows)>1:fail('native owner has multiple disposition inventories; inspect changed evidence')
        if rows:return pin(adapter.workflow.vault,rows[0][0].relative_to(adapter.workflow.vault).as_posix())
    fail('no authenticated ancestor native inventory for this bound work')

def operator_request(adapter,flow,action,operation,inventory):
    w=adapter.workflow.status(flow['workflow_id'])
    result={'workflow_id':w['workflow_id'],'actor':w['actor'],'expected_revision':w['revision'],
        'expected_control_revision':load(adapter.workflow.vault,w['workflow_id'])['revision'],
        'inventory':inventory,'action':action,'operation_id':operation,'reason':flow['reason']}
    result['input_digest']=mutation_digest(result);return result

def native_cycle(adapter,flow,root,label,inventory):
    from build_execution_recovery import contract_hash,SCOPE
    from hermes_source_units.evidence_snapshot import ROOT,rpc,flags
    wid=flow['workflow_id'];vault=adapter.workflow.vault
    w=adapter.workflow.status(wid);verify_cancel_drain(adapter,wid,timeout=0)
    if not adapter.kanban.dispatcher_available():fail('native Gateway must be available before the authorized flow')
    record=lambda name,value:_write_atomic(root/(label+'-'+name+'.json'),_json_bytes(value))
    stage='delivery-'+fingerprint({'operation':flow['operation_id'],'cycle':label})[:32]
    card=next(c for c in w['kanban']['task_map'] if c['node']=='build-finalize')
    failure=vault/f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint("build-finalize")[:16]}.json'
    basis=_load_json(vault/inventory['ref'])['snapshot']
    request={'workflow_id':wid,'actor':w['actor'],'expected_revision':w['revision'],
        'recovery_scope':SCOPE,'recovery_id':stage,'target_contract_sha256':contract_hash(),
        'reason':'Authorized deterministic delivery flow: '+label+'; no source reset, unchanged semantic retry or pause continuation.',
        'entries':[{'node':'build-finalize','task_id':card['task_id'],
            'failure_sha256':hashlib.sha256(failure.read_bytes()).hexdigest(),
            'snapshot_ref':basis['ref'],'snapshot_sha256':basis['sha256'],'retry_unknowns':[]}]}
    request['input_digest']=mutation_digest(request);record('recovery-request',request)
    record('recovery-receipt',adapter.recover_build_execution(request))
    protector=None;started=time.monotonic();state_path=vault/ROOT/wid/'state.json'
    try:
        script=Path(__file__).resolve().parents[1]/'scripts/manage_evidence_snapshot.py'
        with (root/(label+'-protector.stdout.log')).open('ab') as out,(root/(label+'-protector.stderr.log')).open('ab') as err:
            protector=subprocess.Popen([sys.executable,str(script),'serve','--vault',str(vault),'--workflow-id',wid,'--protect-schema'],
                stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
        record('protector',{'pid':protector.pid})
        while time.monotonic()-started<90:
            if state_path.exists() and _load_json(state_path).get('state')=='active':break
            if protector.poll() is not None:fail('evidence protector failed before native dispatch')
            time.sleep(.2)
        else:fail('evidence protector activation timed out')
        record('resume',control_workflow(vault,wid,'resume',stage+'-resume'))
        new=adapter.workflow.status(wid);task=next(c['task_id'] for c in new['kanban']['task_map'] if c['node']=='build-finalize')
        outcome=None
        while time.monotonic()-started<900:
            w=adapter.workflow.status(wid)
            live=workflow_processes(wid,w['kanban']['board_id'])
            if w['pause_control'].get('boundary'):
                outcome={'boundary':w['pause_control']['boundary']};break
            if failure.exists():
                value=_load_json(failure)
                if value.get('task_id')==task and value.get('error_code'):
                    if not any(p['kind'] in ('native_worker','protected_reader') for p in live):
                        outcome={'failure':value};break
            time.sleep(.5)
        if outcome is None:outcome={'timeout':True}
        record('outcome',outcome)
    finally:
        # One stable operation for every exit, including error/timeout. The
        # supported cancel path handles a native holder's lock timeout itself.
        try:
            if adapter.workflow.status(wid)['state']!='cancelled':
                record('cancel',control_workflow(vault,wid,'cancel',stage+'-cancel'))
            record('drain',verify_cancel_drain(adapter,wid))
        finally:
            if protector is not None:
                state=_load_json(state_path)
                if state.get('pid')==protector.pid and state.get('state')=='active':
                    rpc(state['socket'],{'operation':'revoke','reason':'delivery cycle stopped and drained'})
                elif protector.poll() is None:protector.terminate()
                protector.wait(timeout=15)
                manifest=_load_json(state_path.parent/'manifest.json')
                if any(flags(Path(p))!=bits for p,bits in manifest['original_flags'].items()):
                    fail('protector did not restore original evidence flags')
    if outcome.get('timeout'):fail('native cycle timed out; results retained, no automatic retry')
    error=outcome.get('failure',{}).get('error_code')
    if error and error!='SEMANTIC_REVIEW_REQUIRED':fail('native implementation failure retained: '+error)
    return outcome

def run(adapter,request,*,dry_run=False):
    """Explicit user-authorized action; it never runs merely because installed."""
    adapter.workflow._check_request(request)
    if request.get('action')!='run-delivery' or not request.get('reason'):
        fail('explicit run-delivery action and reason required')
    operation=request.get('operation_id','')
    if not __import__('re').fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,60}',operation):fail('stable delivery flow identity required')
    vault=adapter.workflow.vault;wid=request['workflow_id']
    root=vault/f'_system/ledgers/ingest-workflows/{wid}/delivery-operations/{operation}'
    result_path=root/'receipt.json'
    with _exclusive_lock(root/'.lock'):
        if result_path.exists():
            result=_load_json(result_path)
            if result['request_digest']!=request['input_digest']:fail('flow identity changed','IDEMPOTENCY_CONFLICT')
            return {**result,'replayed':True}
        w=adapter.workflow.status(wid)
        if w['actor']!=request['actor'] or w['revision']!=request['expected_revision']:
            fail('flow owner/revision changed','REVISION_CONFLICT')
        from finalize_disposition_control import stopped
        stopped(adapter,w)
        inv=inventory_for(adapter,w)
        from finalize_program_repair import authorize as repairs
        repair_request=operator_request(adapter,request,'authorize-program-repairs',operation+'-repair',inv)
        repair_result=repairs(adapter,repair_request,dry_run=dry_run)
        if dry_run:return {'ok':True,'dry_run':True,'program_repair':repair_result,'maximum_native_cycles':2,
            'next':'automatic native repair if authorized, automatic exclusions, native closure, durable Build pause'}
        _write_atomic(root/'request.json',_json_bytes(request))
        if repair_result['maximum_new_calls']:
            native_cycle(adapter,request,root,'repair',inv)
            inv=inventory_for(adapter,adapter.workflow.status(wid))
        from finalize_delivery_control import authorize as exclude
        closure_request=operator_request(adapter,request,'authorize-delivery',operation+'-scope',inv)
        result=exclude(adapter,closure_request)
        _write_atomic(root/'scope-receipt.json',_json_bytes(result))
        native_cycle(adapter,request,root,'close',inv)
        w=adapter.workflow.status(wid);batch=adapter.workflow.knowledge._batch(w['batch_id'])
        if not batch_complete(adapter.workflow.knowledge,batch) or w['pause_control'].get('boundary')!='build_finalize':
            fail('native delivery closure has not reached the durable Build boundary')
        result={'ok':True,'request_digest':request['input_digest'],'outcome':'closed_with_deferrals',
            'coverage_complete':False,'source_tasks_completed':False,'scope':result['scope'],
            'delivery_closures':closure_refs(adapter.workflow.knowledge,batch['run_ids']),
            'included_pages':result['included_pages'],'excluded_pages':result['excluded_pages'],
            'unknowns_quarantined':result['unknowns_quarantined'],'maximum_program_repair_calls':repair_result['maximum_new_calls'],
            'state':w['state'],'pause':w['pause_control']['boundary'],'publication_executed':False}
        _write_atomic(result_path,_json_bytes(result));return result
