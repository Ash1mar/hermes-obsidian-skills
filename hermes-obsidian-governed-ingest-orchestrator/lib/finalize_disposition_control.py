"""Trusted stopped-workflow Finalize controls; never edits model decisions."""
import copy,json,os,sqlite3,hashlib
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path,_exclusive_lock
from hermes_source_units.validation import fingerprint
from hermes_source_units.partial_finalize import pin,pinned,equivalent_snapshot,progress
from hermes_source_units.finalize_revision_state import load,append,work_id,fail

KINDS={'verified_program_fix','improved_evidence','verified_review_error','authenticated_existing_completion'}

def material_identity(view):
    return fingerprint({k:view.get(k) for k in ('identity','path','existing_page','citation_evidence','original_evidence')})

def stopped(adapter,w):
    from native_stop_inventory import verify_cancel_drain
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('Finalize disposition is operator-only','ACCESS_DENIED')
    if w['state']!='cancelled' or w['current_stage']!='build_finalizing':
        fail('Finalize disposition requires the cancelled Build stage')
    verify_cancel_drain(adapter,w['workflow_id'],timeout=0)

def inspect(adapter,request):
    w=adapter.workflow.status(request['workflow_id']);stopped(adapter,w)
    if w['actor']!=request['actor'] or w['revision']!=request['expected_revision']:
        fail('Finalize operator actor or workflow revision changed','REVISION_CONFLICT')
    inventory=pinned(adapter.workflow.vault,request['inventory'])
    if inventory.get('contract')!='build-evidence-disposition/v1' or inventory['binding']['workflow_id']!=w['workflow_id']:
        fail('foreign Finalize inventory')
    snapshot=pinned(adapter.workflow.vault,inventory['snapshot'])
    import fixed_review
    current=fixed_review.read_snapshot(adapter,snapshot['binding'],{'batch_id':w['batch_id']},True)
    if not equivalent_snapshot(adapter.workflow.knowledge,snapshot,current):
        fail('Finalize domain evidence changed outside a supported revision','STALE_INPUT')
    from build_evidence import check
    checks=check(current)
    if not checks['ok']:fail('common required evidence/mapping checks did not pass','INCOMPLETE_COVERAGE')
    return w,inventory,current,checks

def original_calls(adapter,path,item):
    """Count original body calls from authenticated native user/path identities.

    This is conservative accounting, not permission to reuse their responses.
    Unknown review ordinals are imported as consumed, including no-audit stops.
    """
    vault=adapter.workflow.vault;wid=item['workflow_id'];root=vault/f'_system/ledgers/ingest-workflows/{wid}'
    result={'page-review':item['history']['attempt_count'],'page-write':0};identities=set()
    from revision_disposition import native_user
    from fixed_semantic_recovery import _request_text
    db=sqlite3.connect('file:'+str(Path.home()/'.hermes/state.db')+'?mode=ro',uri=True)
    try:
        for ap in (root/'semantic-calls').glob('*.json'):
            audit=_load_json(ap)
            if audit.get('phase')!='page-write' or audit.get('reused_from_audit'):continue
            model=audit.get('model',{});sid=model.get('session_id')
            if model.get('origin')!='hermes_native_model' or not sid:continue
            if db.execute('SELECT source FROM sessions WHERE id=?',(sid,)).fetchone()!=('kanban',):continue
            users=[native_user(c,audit) for (c,) in db.execute("SELECT content FROM messages WHERE session_id=? AND role='user'",(sid,))]
            for user in users:
                if not user or fingerprint(user)!=audit.get('user_sha256'):continue
                try:view=json.loads(_request_text(user))['input']
                except (ValueError,KeyError,TypeError):continue
                if view.get('path')==path:
                    identities.add(audit.get('output_attempt_id') or sid)
        result['page-write']=len(identities)
    finally:db.close()
    for path_record in (root/'call-failures').glob('*.json'):
        outcome=_load_json(path_record)
        if outcome.get('phase')=='page-write' and outcome.get('view',{}).get('path')==path:
            identities.add(outcome.get('attempt_id') or path_record.name)
    result['page-write']=len(identities)
    return result

def apply(adapter,request,*,dry_run=False):
    service=adapter.workflow;service._check_request(request)
    wid=request['workflow_id'];operation=request.get('operation_id','')
    if not isinstance(operation,str) or not operation or len(operation)>120:fail('stable Finalize operation ID required')
    with _exclusive_lock(service.vault/f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock'),_exclusive_lock(service._lock(wid)):
        state=load(service.vault,wid)
        previous=next((e for e in state['events'] if e['operation_id']==operation),None)
        if previous:
            if previous['metadata'].get('request_digest')!=request['input_digest']:fail('operation ID already bound','IDEMPOTENCY_CONFLICT')
            return {'ok':True,'replayed':True,'control_revision':state['revision'],'resumed':False}
        w,inventory,snapshot,checks=inspect(adapter,request)
        if request.get('expected_control_revision',0)!=state['revision']:fail('Finalize control revision changed','REVISION_CONFLICT')
        action=request.get('action');run_id=request.get('run_id')
        run=next((r for r in snapshot['backend']['runs'] if r['run_id']==run_id),None)
        if run is None:fail('selected run is outside current Build')
        if action in ('enable-partial','prepare-storage'):
            from hermes_source_units.partial_finalize import prepare_storage
            prior=state['works'].get(work_id(run_id,'*'))
            if prior and prior.get('delivery_scope'):
                fail('an authorized delivery scope cannot be replaced by storage/partial enablement')
            if action=='prepare-storage':
                if not prior or prior['state']!='enabled':fail('storage preparation requires the existing partial authorization')
            work={'work_id':work_id(run_id,'*'),'run_id':run_id,'page_id':'*','state':'enabled',
                'run_sha256':fingerprint(_load_json(service.knowledge._run_path(run_id))),
                'basis_inventory':request['inventory'],'checks_sha256':fingerprint(checks),'grants':[]}
            legacy=service.vault/'_system/knowledge-builds/.finalize.lock'
            if legacy.exists() and legacy.read_bytes()==b'' and prior and legacy.relative_to(service.vault).as_posix() in prior.get('storage_prepared',[]):
                work['repaired_empty_lock']={'ref':legacy.relative_to(service.vault).as_posix(),
                    'sha256':hashlib.sha256(b'').hexdigest(),'previous_control_head':state['head']}
            if not dry_run:work['storage_prepared']=prepare_storage(service.knowledge,w,prior)
        else:
            item=next((i for i in inventory['items'] if i['issue_id']==request.get('issue_id') and i['logical_work']['run_id']==run_id),None)
            if item is None:fail('exact original Finalize issue required')
            pid=item['logical_work']['page_id'];key=work_id(run_id,pid)
            view=next(v for v in snapshot['views'] if v['page']==item['page'])
            work=copy.deepcopy(state['works'].get(key) or {'work_id':key,'run_id':run_id,'page_id':pid,
                'original_revision_id':item['logical_work']['revision_id'],'issue_ids':[], 'grants':[],
                'baseline_calls':original_calls(adapter,view['path'],{**item,'workflow_id':wid}),
                'latest_revision':None,'state':'held'})
            if item['issue_id'] not in work['issue_ids']:work['issue_ids'].append(item['issue_id'])
            work['latest_inventory']=request['inventory'];work['observed_status']=item['observed_status']
            unknown=item['observed_status'] in ('completion_unknown','unproven_stopped_attempt')
            if action=='defer':
                if unknown:fail('execution uncertainty cannot be deferred as a content issue')
                if item['state']!='deferred':fail('a valid ready result cannot be silently deferred')
                if request.get('category') not in ('source_conversion','model_content','review_dispute','unresolved'):
                    fail('explicit deferral category required')
                work.update(state='deferred',category=request['category'],reason=request.get('reason',''),attribution_confirmed=False)
            elif action=='hold':
                work.update(state='held',reason=request.get('reason',''))
            elif action in ('reopen','revise'):
                basis=request.get('basis',{})
                if basis.get('kind') not in KINDS or not basis.get('reason') or not basis.get('evidence'):
                    fail('reopening requires explicit new checked evidence or disposition basis')
                evidence=[pin(service.vault,p['ref']) for p in basis['evidence']]
                if any(a!=b for a,b in zip(evidence,basis['evidence'])):fail('new basis evidence hash changed','SOURCE_CHANGED')
                if request['inventory'] in evidence or inventory['snapshot'] in evidence:
                    fail('the unchanged original inventory is not new reopening evidence')
                if unknown:
                    if basis['kind']!='authenticated_existing_completion' or action!='reopen':
                        fail('unknown completion requires proof, never a content retry')
                    from finalize_revision import certify_existing
                    work['certificate']=certify_existing(adapter,snapshot,item,basis,work=work)
                    work.update(state='open',basis=basis)
                elif action=='reopen':
                    if work['state'] not in ('deferred','held'):fail('only a held/deferred issue may reopen')
                    work.update(state='open',basis=basis,material_sha256=material_identity(view))
                else:
                    if work['state']!='open' or work.get('basis')!=basis:fail('revise requires its exact explicitly reopened basis')
                    if pid in progress(service.knowledge,_load_json(service.knowledge._run_path(run_id)))['committed']:
                        fail('this local revision path preserves already committed valid pages')
                    material=material_identity(view)
                    mode=request.get('mode','rewrite_and_review')
                    if mode=='rewrite_and_review' and not work['baseline_calls'].get('page-write'):
                        fail('original page-generation call history must be certified before granting a revision')
                    if any(g['material_sha256']==material for g in work['grants']):
                        fail('this logical work already consumed its bounded cycle for unchanged material')
                    if mode not in ('review_only','rewrite_and_review'):fail('unsupported local revision mode')
                    grant={'grant_id':fingerprint({'work':key,'material':material,'basis_kind':basis['kind']}),
                        'authorized_control_revision':state['revision']+1,
                        'material_sha256':material,'basis':basis,'mode':mode,
                        'limits':{'page-write':int(mode=='rewrite_and_review'),'page-review':1},'calls':{}}
                    if request.get('evidence_encoding'):
                        if request['evidence_encoding']!='lossless-page-evidence/v2' or basis['kind']!='verified_program_fix':
                            fail('encoding change requires the verified lossless program fix')
                        grant['evidence_encoding']=request['evidence_encoding']
                        grant['review_scope']={'policy':view.get('review_scope',{}).get('policy','explicit-page-risks/v1'),
                            'risk_reasons':item['evidence']['assessment']['risk_reasons'],
                            'instruction':'Review only assigned page risks against all supplied originals; retain every necessary condition, citation and QA limit.'}
                    work['grants'].append(grant);work['state']='revision_authorized'
            else:fail('supported actions: enable-partial, defer, hold, reopen, revise')
        result={'ok':True,'action':action,'work':work,'evidence_checks':checks,'dry_run':dry_run,
            'resumed':False,'pause_released':False,'new_calls_granted':sum(work['grants'][-1]['limits'].values()) if action=='revise' else 0}
        if not dry_run:
            state,event=append(service.vault,wid,work,operation,action,{'request_digest':request['input_digest'],
                'actor':w['actor'],'inventory':request['inventory'],'reason':request.get('reason','')})
            result['control_revision']=state['revision']
        return result
