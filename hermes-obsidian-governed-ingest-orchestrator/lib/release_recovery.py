"""Replan a stopped unbound release after an authenticated governance change."""
import hashlib
import os
import re
from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock,_vault_path,_load_json,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint,record_validation_scope
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from native_stop_inventory import verify_cancel_drain

def recover(adapter,request,*,dry_run=False):
    if request.get('recovery_scope')=='provider-disabled/v1':
        from provider_disposition_control import prepare
        return prepare(adapter,request,dry_run=dry_run)
    def fail(message):raise ContractError('RELEASE_RECOVERY_NOT_ALLOWED','$',message)
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):fail('operator only')
    service=adapter.workflow;service._check_request(request);wid=request['workflow_id'];rid=request.get('recovery_id','')
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,79}',rid) or not request.get('reason','').strip():fail('stable identity and reason required')
    with _exclusive_lock(_vault_path(service.vault,f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock')), _exclusive_lock(service._lock(wid)),record_validation_scope():
        w=service.status(wid);service.assert_execution_ready(w)
        if w['actor']!=request['actor']:fail('actor differs')
        history=w.get('release_recovery_history',[])
        for old in history:
            if old['recovery_id']==rid:
                if old['input_digest']!=request['input_digest']:fail('operation conflict')
                return {'ok':True,'replayed':True,'resumed':False,'report_ref':old['report_ref']}
        w=service._mutation(request)
        if w['state']!='cancelled' or not w['cancel_requested'] or w['current_stage']!='release_planning' or service.pending_pause(w) or w['release_id']:fail('requires stopped unbound release planning')
        verify_cancel_drain(adapter,wid,timeout=0)
        from ingest_kanban import desired_graph,domain_completed
        nodes={n.name:n for n in desired_graph(service,w)}
        if not domain_completed(service,w,nodes['build-finalize']):fail('Build closure changed')
        node=nodes['vault-finalize-plan'];card=next(c for c in w['kanban']['task_map'] if c['node']==node.name)
        if request.get('task_id')!=card['task_id'] or request.get('input_fingerprint')!=node.input_fingerprint:fail('native binding changed')
        path=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(node.name)[:16]}.json')
        data=path.read_bytes();failure=_load_json(path)
        if hashlib.sha256(data).hexdigest()!=request.get('failure_sha256') or failure.get('task_id')!=card['task_id'] or failure.get('error_code')!='STALE_INPUT':fail('exact rejected plan failure required')
        oldpath=_vault_path(service.vault,request['blocked_plan']['ref']);olddata=oldpath.read_bytes();plan=_load_json(oldpath)
        expected=FileVaultFinalizeService(service.vault)._plan_path('release-'+fingerprint({'workflow_id':wid,'node':node.name,'input_fingerprint':node.input_fingerprint})[:24])
        scope=request.get('recovery_scope','governance-resolved/v1')
        allowed=(plan['blockers']==['no-deliverable-pages'] if scope=='governance-resolved/v1' else
            bool(plan['blockers']) and all(b.startswith('ambiguous-alias:') for b in plan['blockers']) if scope=='unused-navigation-alias/v1' else False)
        if oldpath!=expected or hashlib.sha256(olddata).hexdigest()!=request['blocked_plan']['sha256'] or not allowed or plan['state']!='draft':fail('exact blocked plan and supported cause required')
        if scope=='unused-navigation-alias/v1':
            from hermes_source_units.vault_finalize import _WIKILINK
            labels={b.split(':',2)[1] for b in plan['blockers']}
            p4=FileVaultFinalizeService(service.vault)
            for subject in plan['subjects']:
                page=p4._page_revision(subject['page_id'],subject['current_revision_id'])
                data=_vault_path(service.vault,subject['current_path']).read_bytes()
                if hashlib.sha256(data).hexdigest()!=page['authored_sha256']:fail('selected page changed')
                if any(raw.strip().casefold() in labels for raw in _WIKILINK.findall(data.decode('utf-8'))):fail('an ambiguous label is actually referenced')
        native=adapter.kanban.task_snapshot_fresh(w['kanban']['board_id'],card['task_id'])
        if not native['runs'] or any(not r.get('ended_at') for r in native['runs']):fail('native attempts must be ended')
        receipt_path=_vault_path(service.vault,request['governance_receipt']['ref']);receipt_data=receipt_path.read_bytes();receipt=_load_json(receipt_path)
        if hashlib.sha256(receipt_data).hexdigest()!=request['governance_receipt']['sha256'] or not receipt.get('applied') or not receipt.get('resolved'):fail('completed governance resolution required')
        transaction=_load_json(receipt_path.with_name('transaction.json'))
        if transaction['request']['workflow_id']!=wid or transaction['request']['actor']!=w['actor']:fail('governance authority differs')
        operation=transaction['request']['operation_id']
        if receipt_path!=_vault_path(service.vault,f'_system/metadata/source-governance-resolutions/{operation}/receipt.json') or receipt.get('operation_id')!=operation:fail('governance receipt identity differs')
        q=transaction['request']
        if q.get('automatic_approval_activation') is not True or q['input_digest']!='sha256:'+fingerprint({k:v for k,v in q.items() if k!='input_digest'}):fail('governance authorization changed')
        resources={r['resource_id'] for r in receipt['rows'] if r['resolved']}
        p4=FileVaultFinalizeService(service.vault);eligibility=p4._source_eligibility(p4._current_sets(),{})
        if not resources or not resources<={e['resource_id'] for e in eligibility} or any(not e['eligible'] for e in eligibility if e['resource_id'] in resources):fail('resolved sources still ineligible')
        if request.get('initial_page_governance'):
            if scope!='governance-resolved/v1':fail('initial page scope requires source-governance recovery')
            from hermes_source_units.initial_release import validate as validate_initial,admitted_pointer
            if admitted_pointer(service.vault,w):fail('initial page authority was already admitted')
            validate_initial(p4,w,request['initial_page_governance'],blocked_plan=request['blocked_plan'],
                governance_receipt=request['governance_receipt'])
        if any(r.get('previous_task_id')==card['task_id'] for r in [_load_json(_vault_path(service.vault,h['report_ref'])) for h in history]):fail('same failed work already recovered')
        ref=f'_system/ledgers/ingest-workflows/{wid}/release-recoveries/{rid}.json'
        report={'request':dict(request),'previous_task_id':card['task_id'],'failure':failure,'native_runs':native['runs'],
                'blocked_plan':request['blocked_plan'],'governance_receipt':request['governance_receipt'],'eligibility':eligibility,'domain_calls_granted':0,'resumed':False}
        result={'ok':True,'applied':not dry_run,'resumed':False,'report_ref':ref,'domain_calls_granted':0}
        if dry_run:return result
        payload=_json_bytes(report);dest=_vault_path(service.vault,ref)
        if dest.exists() and dest.read_bytes()!=payload:fail('recovery audit conflicts')
        if not dest.exists():_write_atomic(dest,payload)
        w.setdefault('release_recovery_history',[]).append({'recovery_id':rid,'input_digest':request['input_digest'],
            'report_ref':ref,'report_sha256':hashlib.sha256(payload).hexdigest()})
        w['revision']+=1;service._write(w);return result
