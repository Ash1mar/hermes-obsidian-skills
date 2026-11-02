"""Recover only a failed scoped checkpoint after program impact classification repair."""
import hashlib,os,re
from hermes_source_units import ContractError
from hermes_source_units.source_units import _exclusive_lock,_vault_path,_load_json,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint,record_validation_scope
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from hermes_source_units.release_lint import impact_context,classify
from native_stop_inventory import verify_cancel_drain

def recover(adapter,request,*,dry_run=False):
    def fail(message):raise ContractError('RELEASE_VALIDATION_RECOVERY_NOT_ALLOWED','$',message)
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):fail('operator only')
    service=adapter.workflow;service._check_request(request);wid=request['workflow_id'];rid=request.get('recovery_id','')
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,79}',rid) or not request.get('reason','').strip():fail('stable operation and reason required')
    with _exclusive_lock(_vault_path(service.vault,f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock')),_exclusive_lock(service._lock(wid)),record_validation_scope():
        w=service.status(wid);service.assert_execution_ready(w)
        if w['actor']!=request['actor']:fail('actor mismatch')
        history=w.get('release_validation_history',[])
        for h in history:
            if h['recovery_id']==rid:
                if h['input_digest']!=request['input_digest']:fail('operation conflict')
                return {'ok':True,'replayed':True,'resumed':False,'report_ref':h['report_ref']}
        w=service._mutation(request)
        if w['state']!='cancelled' or not w['cancel_requested'] or w['current_stage']!='release_planning' or not w['release_id'] or service.pending_pause(w):fail('requires stopped bound release before checkpoint')
        verify_cancel_drain(adapter,wid,timeout=0)
        from ingest_kanban import desired_graph
        node=next(n for n in desired_graph(service,w) if n.name=='checkpoint-2-validate')
        card=next(c for c in w['kanban']['task_map'] if c['node']==node.name)
        if request.get('task_id')!=card['task_id'] or request.get('input_fingerprint')!=node.input_fingerprint:fail('checkpoint binding changed')
        native=adapter.kanban.task_snapshot_fresh(w['kanban']['board_id'],card['task_id'])
        if not native['runs'] or any(not r.get('ended_at') for r in native['runs']):fail('native runs must be ended')
        failure_path=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(node.name)[:16]}.json')
        if hashlib.sha256(failure_path.read_bytes()).hexdigest()!=request.get('failure_sha256'):fail('failure changed')
        failure=_load_json(failure_path)
        if failure.get('input_fingerprint')!=node.input_fingerprint or failure.get('report_ref')!=request['lint_report']['ref']:fail('failure is not this lint')
        plan_path=FileVaultFinalizeService(service.vault)._plan_path(w['release_id']);plan=_load_json(plan_path)
        if plan['plan_id']!=w['release_plan_id'] or plan['blockers'] or not plan.get('delivery_scopes'):fail('exact scoped bound plan required')
        lint_path=_vault_path(service.vault,request['lint_report']['ref']);lint=_load_json(lint_path)
        if hashlib.sha256(lint_path.read_bytes()).hexdigest()!=request['lint_report']['sha256'] or lint.get('ok'):fail('exact failed lint required')
        p4=FileVaultFinalizeService(service.vault);corrected=classify(plan,lint,impact_context(p4,plan,lint))
        if not corrected['ok']:fail('material or unclassified errors remain')
        if any(_load_json(_vault_path(service.vault,h['report_ref']))['previous_task_id']==card['task_id'] for h in history):fail('same checkpoint already recovered')
        ref=f'_system/ledgers/ingest-workflows/{wid}/release-validation-recoveries/{rid}.json'
        report={'request':dict(request),'previous_task_id':card['task_id'],'failure':failure,'lint':lint,
                'corrected_impact':corrected,'plan_ref':plan_path.relative_to(service.vault).as_posix(),
                'plan_sha256':hashlib.sha256(plan_path.read_bytes()).hexdigest(),'plan_id':plan['plan_id'],'plan_snapshot':plan,'native_runs':native['runs'],'domain_calls_granted':0,'resumed':False}
        result={'ok':True,'applied':not dry_run,'resumed':False,'report_ref':ref,'plan_reused':True,'domain_calls_granted':0}
        if dry_run:return result
        path=_vault_path(service.vault,ref);payload=_json_bytes(report)
        if path.exists() and path.read_bytes()!=payload:fail('immutable recovery conflicts')
        for suffix,original in [('lint',lint_path),('failure',failure_path)]:
            audit=path.with_name(path.stem+'-'+suffix+'-before.json');data=original.read_bytes()
            if audit.exists() and audit.read_bytes()!=data:fail('original audit conflict')
            if not audit.exists():_write_atomic(audit,data)
        if not path.exists():_write_atomic(path,payload)
        w.setdefault('release_validation_history',[]).append({'recovery_id':rid,'input_digest':request['input_digest'],'report_ref':ref,'report_sha256':hashlib.sha256(payload).hexdigest()})
        w['revision']+=1;service._write(w);return result
