"""Stopped operator authorization of exclusions; native workers close delivery."""
import copy,hashlib,re
from hermes_source_units import mutation_digest
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes,_exclusive_lock
from hermes_source_units.validation import fingerprint
from hermes_source_units.partial_finalize import pin,pinned,progress
from hermes_source_units.delivery_scope import CONTRACT,dependency_partition,fail
from hermes_source_units.finalize_revision_state import load,append,work_id
from finalize_disposition_control import inspect

CATEGORIES={'unresolved','source_conversion','model_content','review_dispute','material_insufficient','input_limit'}

def automatic_dispositions(inventory):
    result=[]
    for item in inventory['items']:
        if item['state']!='deferred':continue
        status=item['observed_status'];code=(item.get('diagnostic') or {}).get('code')
        category=('input_limit' if code=='READING_WINDOW_OVERSIZE' else
                  'material_insufficient' if status=='explicit_refusal' else 'unresolved')
        result.append({'issue_id':item['issue_id'],'category':category,'attribution_confirmed':False,
            'reason':'Observed '+status+'; preserve the exact inventory, request, response and diagnostic. Final semantic cause remains unconfirmed.',
            'reopen_condition':('Authenticate an existing completion or verify ended owners and quarantine late results under a new delivery scope.'
                if status in ('completion_unknown','unproven_stopped_attempt') else
                'New verified program correction, materially improved evidence, or authenticated review correction; unchanged evidence grants zero further calls.')})
    return result

def quarantine(adapter,w,item):
    """Prove ended local owners; retain remote completion uncertainty verbatim."""
    journal=item.get('history',{}).get('journal')
    if not journal:fail('unknown execution lacks its pinned original journal')
    record=pinned(adapter.workflow.vault,journal);owners=[]
    for attempt in record['attempts']:
        tid=attempt.get('task_id')
        if not tid:fail('unknown execution has no native owner identity')
        native=adapter.kanban.task_snapshot_fresh(w['kanban']['board_id'],tid)
        task=native.get('task',{});runs=native.get('runs',[])
        if task.get('id')!=tid or task.get('worker_pid') or not runs or any(not r.get('ended_at') for r in runs):
            fail('uncertain invocation owner has not verifiably ended','WORKER_ACTIVE')
        owners.append({'task_id':tid,'task_state':task['status'],
            'ended_runs':[{'id':str(r['id']),'ended_at':str(r['ended_at'])} for r in runs],
            'native_snapshot_sha256':hashlib.sha256(_json_bytes(native)).hexdigest(),
            'attempt_id':attempt['attempt_id'],'wire_sha256':attempt.get('wire_sha256'),
            'session_id':attempt.get('session_id'),'audit_ref':attempt.get('audit_ref')})
    if not owners:fail('unknown invocation has no ended ordinal')
    return {'journal':journal,'owners':owners,'local_invocations_stopped':True,
        'completion_state':item['observed_status'],'remote_completion_certified':False,
        'late_result_policy':'excluded from this delivery even if a late response arrives; authenticate and reopen under new evidence and a new delivery scope',
        'new_calls_granted':0}

def authorize(adapter,request,*,dry_run=False):
    service=adapter.workflow;wid=str(request['workflow_id']);service._check_request(request)
    operation=request.get('operation_id','')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',operation)
            or not request.get('reason') or request.get('action')!='authorize-delivery'):
        fail('delivery authorization needs a stable operation, action and reason')
    root=service.vault/f'_system/ledgers/ingest-workflows/{wid}/finalize-controls'
    with _exclusive_lock(root/'.operator.lock'),_exclusive_lock(service._lock(wid)):
        state=load(service.vault,wid)
        old=[e for e in state['events'] if e['operation_id'].startswith(operation+':')]
        if old and any(e['metadata'].get('request_digest')!=request['input_digest'] for e in old):
            fail('delivery operation ID is already bound','IDEMPOTENCY_CONFLICT')
        w,inventory,snapshot,checks=inspect(adapter,request)
        if not old and request.get('expected_control_revision')!=state['revision']:
            fail('Finalize control revision changed','REVISION_CONFLICT')
        batch=service.knowledge._batch(w['batch_id'])
        if set(batch['run_ids'])!={r['run_id'] for r in snapshot['backend']['runs']}:
            fail('delivery must account for every original batch run')
        deferred={i['issue_id']:i for i in inventory['items'] if i['state']=='deferred'}
        from finalize_program_repair import candidates as repair_candidates
        if repair_candidates(adapter,w,inventory,snapshot)['eligible']:
            fail('automatic bounded program repairs remain; execute the authorized repair admission before excluding them')
        decision_rows=request.get('decisions') or automatic_dispositions(inventory)
        decisions={d['issue_id']:d for d in decision_rows}
        if len(decisions)!=len(decision_rows) or set(decisions)!=set(deferred):
            fail('every held/unknown issue must receive exactly one explicit disposition')
        excluded={};proofs={}
        for issue,item in deferred.items():
            d=decisions[issue]
            if d.get('category') not in CATEGORIES or not d.get('reason') or not d.get('reopen_condition'):
                fail('each exclusion needs a category, evidence reason and reopening condition')
            if d.get('attribution_confirmed',False):
                fail('this pending-attribution path does not certify a semantic cause')
            pid=item['logical_work']['page_id']
            excluded[pid]={**copy.deepcopy(d),'observed_status':item['observed_status'],
                'attribution_confirmed':False,'evidence':{'inventory':request['inventory'],
                'journal':item.get('history',{}).get('journal'),'native_audit':item.get('native_audit')},
                'new_calls_granted':0}
            if item['observed_status'] in ('completion_unknown','unproven_stopped_attempt'):
                if d['category']!='unresolved':fail('execution unknown must retain its independent classification')
                proofs[pid]=quarantine(adapter,w,item)
                excluded[pid]['category']='execution_quarantined'
                excluded[pid]['quarantine']=proofs[pid]
        pages=[]
        for view in snapshot['views']:
            row=snapshot['backend']['pages'][view['page']]
            pages.append({'page_id':row['page']['page_id'],'path':view['path'],
                'identity':view['identity'],'content':view['content']})
        partition=dependency_partition(pages,excluded)
        run_rows=[];included=[];excluded_rows=[]
        for run in snapshot['backend']['runs']:
            original=_load_json(service.knowledge._run_path(run['run_id']))
            current=progress(service.knowledge,original)
            if current['pending']:fail('an interrupted commit must finish before scope exclusion')
            run_rows.append({'run_id':run['run_id'],'manifest':pin(service.vault,service.knowledge._run_path(run['run_id']).relative_to(service.vault).as_posix()),
                'tasks':[pin(service.vault,service.knowledge._task_path(t['task_id']).relative_to(service.vault).as_posix()) for t in run['tasks']]})
            for page in run['page_revisions']:
                row={'run_id':run['run_id'],**{k:page[k] for k in ('page_id','revision_id','authored_sha256','path')}}
                if page['page_id'] in partition['excluded']:
                    excluded_rows.append({**row,**partition['excluded'][page['page_id']]})
                else:
                    if page['page_id'] not in current['committed']:fail('included page has not passed canonical native submission')
                    included.append(row)
        if not included:fail('delivery has no independently valid page')
        from hermes_source_units.reduce_coverage import batch_outcome
        coverage=batch_outcome(service.knowledge,batch)
        scope={'contract':CONTRACT,'workflow_id':wid,'batch_id':batch['batch_id'],'actor':w['actor'],
            'control_revision':state['revision'],
            'operation_id':operation,'request_digest':request['input_digest'],'inventory':request['inventory'],
            'snapshot':inventory['snapshot'],'evidence_checks_sha256':fingerprint(checks),
            'runs':run_rows,'included':included,'excluded':excluded_rows,
            'dependencies':partition['dependencies'],'coverage_gaps':coverage,
            'outcome':'authorized_delivery_with_deferrals','coverage_complete':False,
            'source_tasks_completed':False,'new_domain_calls':0,'reason':request['reason']}
        scope['scope_id']=fingerprint(scope)
        ref=f'_system/ledgers/ingest-workflows/{wid}/delivery-scopes/{scope["scope_id"]}.json'
        pointer={'ref':ref,'sha256':hashlib.sha256(_json_bytes(scope)).hexdigest()}
        if old and any(e['work'].get('delivery_scope')!=pointer for e in old):
            fail('a partially persisted delivery authorization differs','SOURCE_CHANGED')
        if not dry_run:
            path=_vault_path(service.vault,ref)
            if path.exists() and _load_json(path)!=scope:fail('scope record changed','SOURCE_CHANGED')
            if not path.exists():_write_atomic(path,_json_bytes(scope))
            for run in run_rows:
                key=work_id(run['run_id'],'*');work=copy.deepcopy(state['works'].get(key,{}))
                if not work or work['state']!='enabled':fail('partial submission authorization required')
                work['delivery_scope']=pointer
                state,_=append(service.vault,wid,work,operation+':'+fingerprint(run['run_id'])[:12],
                    'authorize-delivery',{'request_digest':request['input_digest'],'actor':w['actor'],
                    'inventory':request['inventory'],'reason':request['reason']})
        return {'ok':True,'dry_run':dry_run,'scope':pointer,'scope_id':scope['scope_id'],
            'included_pages':len(included),'excluded_pages':len(excluded_rows),'unknowns_quarantined':len(proofs),
            'inherited_coverage_gaps_preserved':bool(coverage),'coverage_complete':False,
            'new_domain_calls':0,'resumed':False,'pause_released':False,'control_revision':state['revision']}
