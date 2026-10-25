"""Operator control for the bounded native citation → local Reduce → pause chain."""
import copy
import hashlib
from pathlib import Path
from hermes_source_units import mutation_digest
from hermes_source_units.citation_amendment import (SCOPE,fail,root,state,plan,save,row,transaction,replay,pending_calls)
from hermes_source_units.source_units import _load_json,_vault_path,_json_bytes,_write_atomic,_exclusive_lock
from hermes_source_units.validation import fingerprint

def contract_hash():
    import hermes_source_units.citation_amendment as core
    names=['citation_amendment_control.py','citation_amendment_worker.py','citation_amendment_calls.py',
           'citation_amendment_admission.py','citation_local_proof.py','fixed_semantic.py','fixed_reduce.py',
           'reduce_coverage.py','hermes_source_units/pass_revision.py','hermes_source_units/knowledge_build.py',
           'hermes_source_units/workflow_guard.py','hermes_source_units/reduce_dependency_revision.py',
           'hermes_source_units/model_projection.py','hermes_source_units/execution_plan.py',
           'hermes_source_units/schemas/contracts.json']
    return fingerprint({**{n:hashlib.sha256((Path(__file__).parent/n).read_bytes()).hexdigest() for n in names},
                        'core':hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest()})

def mutation(w,**fields):
    req={'workflow_id':w['workflow_id'],'actor':w['actor'],'expected_revision':w['revision'],**fields}
    req['input_digest']=mutation_digest(req);return req

def drained(adapter,w):
    from native_stop_inventory import verify_cancel_drain
    verify_cancel_drain(adapter,w['workflow_id'],timeout=0)
    if any(s['state']=='leased' or s['lease']['worker_id'] for s in adapter.workflow.knowledge._slices(w['batch_id'])):
        fail('stop all leases before amendment control changes')

def prepare(adapter,request,dry_run):
    service=adapter.workflow;wid=request['workflow_id'];w=service._mutation(request);drained(adapter,w)
    from hermes_source_units.reduce_coverage import active
    scope=active(service.vault,w)
    if (w['current_stage']!='checkpoint_1' or service.pending_pause(w)!='checkpoint_1' or not scope
            or any(p['boundary']=='checkpoint_1' for p in w['pause_control']['history'])):
        fail('first amendment version requires an unreleased limited checkpoint_1')
    current=state(service.vault,wid)
    if current and current['phase']!='complete':fail('finish the current amendment; another ID cannot fork its authority')
    base=root(service.vault,wid);op=request['recovery_id'];path=base/op/'plan.json'
    if path.exists():fail('operation identity already used; replay its existing state')
    from hermes_source_units.citation_revision_impact import preview
    selections=request.get('selections',[])
    if len(selections)!=1:fail('bounded first version selects exactly one citation task')
    impact=preview(service,w,selections);selection=selections[0]
    if sum(s['requires_revalidation'] for s in impact['sources'])!=1:
        fail('bounded first version requires one consumed source result for the selected task')
    basis=request.get('basis',[])
    if not basis or len(basis)>8 or not request.get('reason','').strip():fail('confirmed correction needs pinned evidence and a reason')
    for b in basis:
        if hashlib.sha256(_vault_path(service.vault,b['ref']).read_bytes()).hexdigest()!=b['sha256']:fail('problem evidence changed')
    budgets=request.get('budgets',{});limits=request.get('cumulative_limits',{})
    caps={'citation':2,'source':2,'plan':2,'body':4}
    if set(budgets)!=set(caps) or set(limits)!=set(caps) or any(
            type(budgets[p]) is not int or type(limits[p]) is not int or not 0<=budgets[p]<=limits[p]<=caps[p] for p in caps) or budgets.get('citation')!=1:
        fail('explicit phase budgets and cumulative limits are required; no default fresh attempts')
    ledger=pending_calls(service.vault,wid);lineage=selection['task_id']
    if lineage in ledger.get('limits',{}) and ledger['limits'][lineage]!=limits:fail('operation/version changes cannot reset cumulative limits')
    for p in caps:
        spent=sum(a['lineage']==lineage and a['phase']==p for j in ledger['jobs'].values() for a in j['attempts'])
        if spent+budgets[p]>limits[p]:fail('requested authority exceeds remaining cumulative lineage budget')
    # Accepted native evidence is sealed, not re-reviewed semantically.
    import reduce_coverage
    source=_load_json(_vault_path(service.vault,scope['manifest']['source_snapshot_ref']))
    reduce_coverage.reuse_pages(adapter,source,scope)
    k=service.knowledge;batch=k._batch(w['batch_id'])
    for run_id in batch['run_ids']:k.validate_run(run_id)
    prior_files=[p for p in (service.vault/f'_system/knowledge-builds/task-{selection["task_id"]}/passes').glob('*.json')
                 if _load_json(p)['pass_id']==selection['pass_id']]
    if len(prior_files)!=1:fail('one exact predecessor record is required')
    histories=[]
    for p in (service.vault/f'_system/ledgers/ingest-workflows/{wid}/output-attempts').glob('*.json'):
        r=_load_json(p)
        histories.append({'ref':p.relative_to(service.vault).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
                          'attempt_count':len(r['attempts']),'unknown_count':sum(a.get('state')=='reserved' for a in r['attempts'])})
    frozen={'contract':SCOPE,'workflow_id':wid,'batch_id':w['batch_id'],'actor':w['actor'],
        'selection':selection,'reason':request['reason'],'basis':basis,'budgets':budgets,'cumulative_limits':limits,
        'scope':scope,'old_source':source,'old_pass_ref':prior_files[0].relative_to(service.vault).as_posix(),
        'old_pass_sha256':hashlib.sha256(prior_files[0].read_bytes()).hexdigest(),
        'retired_source_ids':[r['reduction_id'] for r in impact['sources'] if r['requires_revalidation']],
        'sources':[k._resource_reduction(batch['batch_id'],rid) for rid in batch['resource_reduction_ids']],
        'impact':impact,'original_call_history':histories,'original_pause':w['pause_control'],
        'previous_operation_id':current['operation_id'] if current else None,'request':request}
    if dry_run:return {'ok':True,'applied':False,'resumed':False,'impact':impact,'budgets':budgets}
    value={'operation_id':op,'phase':'prepared','revision_id':'citation-'+op,'plan_ref':path.relative_to(service.vault).as_posix(),
           'plan_sha256':hashlib.sha256(_json_bytes(frozen)).hexdigest(),'steps':{},'downstream_nodes':[]}
    ledger.setdefault('limits',{})[lineage]=limits
    audit_root=service.vault/f'_system/ledgers/ingest-workflows/{wid}'
    call_evidence=[]
    for folder in ('semantic-calls','call-failures'):
        for p in (audit_root/folder).glob('*.json'):
            data=_load_json(p)
            if folder=='call-failures' or selection['task_id'] in data.get('task_identities',{}).values():
                call_evidence.append({'ref':p.relative_to(service.vault).as_posix(),
                                      'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
                                      'state':data.get('state','native_response_recorded'),
                                      'session_id':data.get('session_id',data.get('model',{}).get('session_id'))})
    ledger.setdefault('baselines',{}).setdefault(lineage,{'original_journal_prefixes':histories,
        'native_and_unknown_call_evidence':call_evidence,'unknown_usage_preserved':True,
        'limits_are_explicit_additional_authority':True,'complete_historical_usage_known':False})
    if current:
        _write_atomic(base/current['operation_id']/'completed-state.json',_json_bytes(current))
    transaction(service.vault,wid,[row(service.vault,path,frozen),row(service.vault,base/'budgets.json',ledger),
                                 row(service.vault,base/'active.json',value)],op+':prepare')
    return {'ok':True,'applied':True,'resumed':False,'phase':'prepared','operation_id':op}

def stored_step(service,wid,value,name,factory):
    if name not in value['steps']:
        value['steps'][name]=factory();save(service.vault,wid,value)
    return value['steps'][name]

def start(adapter,request):
    service=adapter.workflow;wid=request['workflow_id'];value=state(service.vault,wid);frozen=plan(service.vault,value)
    w=service.status(wid);drained(adapter,w)
    if value['phase'] not in ('prepared','citation'):fail('start requires the prepared citation amendment')
    from reduce_coverage_reopen import recover
    from reduce_coverage import contract_hash as coverage_hash
    reopen=stored_step(service,wid,value,'reopen',lambda:mutation(w,recovery_id=value['operation_id']+'-withdraw',
        recovery_scope='reduce-coverage-reopen/v1',target_contract_sha256=coverage_hash(),
        coverage_scope=frozen['scope']['ref'],basis=frozen['basis'],reason=frozen['reason']))
    recover(adapter,reopen)
    w=service.status(wid);k=service.knowledge;batch=k._batch(w['batch_id'])
    if value['phase']=='prepared':
        newbatch=copy.deepcopy(batch)
        newbatch.update(resource_reduction_ids=[rid for rid in batch['resource_reduction_ids'] if rid not in frozen['retired_source_ids']],
                        revision=batch['revision']+1,last_operation='amend-execution')
        value['phase']='citation'
        value['downstream_nodes']=['resource-reduce:'+r['resource_id'] for r in frozen['sources']
                                   if r['reduction_id'] in frozen['retired_source_ids']]+['global-reduce']
        transaction(service.vault,wid,[row(service.vault,k._batch_path(batch['batch_id']),newbatch),
            row(service.vault,root(service.vault,wid)/'active.json',value)],value['operation_id']+':retire')
    revision=stored_step(service,wid,value,'citation_request',lambda:mutation(service.status(wid),
        revision_id=value['revision_id'],revision_scope='reduce-dependencies/v1',amendment_id=value['operation_id'],
        evidence_digest='sha256:'+fingerprint(service.status(wid)['pause_control']),
        tasks=[{'task_id':frozen['selection']['task_id'],'pass_id':frozen['selection']['pass_id'],'reason':frozen['reason']}],
        reason=frozen['reason'],evidence_refs=[b['ref'] for b in frozen['basis']],
        worker_template=(Path(__file__).parent.parent/'references/workers/pass-slice.md').read_text()))
    adapter.revise_passes(revision)
    return {'ok':True,'phase':'citation','resumed':False,'operation_id':value['operation_id']}

def accept_successor(adapter,request):
    service=adapter.workflow;wid=request['workflow_id'];value=state(service.vault,wid);frozen=plan(service.vault,value)
    w=service.status(wid)
    if value['phase'] not in ('citation','successor_verified','downstream'):fail('successor review is outside its phase')
    if value['phase']=='downstream':return {'ok':True,'phase':'downstream','resumed':False}
    from citation_amendment_calls import authenticate
    selection=frozen['selection'];records=service.knowledge._pass_index([selection['task_id']])[1][selection['task_id']]
    matches=[p for p in records if p.get('revision_id')==value['revision_id']]
    if len(matches)!=1:fail('one durable native successor is required')
    successor=matches[0];old=_load_json(_vault_path(service.vault,frozen['old_pass_ref']))
    from hermes_source_units.citation_revision_impact import successor_delta
    delta=successor_delta(old,successor,selection['candidate_ids'])
    calls=authenticate(adapter,wid,'citation')
    if len(calls)!=1:fail('one authenticated native citation call is required')
    # Reconstruct the actual native review against the original short-ref map;
    # native Pass submission already enforces package/scope/inspection checks.
    from citation_amendment_calls import verify_citation_body
    verify_citation_body(adapter,frozen,successor,calls[0])
    value.update(phase='successor_verified',successor=successor,delta=delta,citation_proofs=calls)
    save(service.vault,wid,value)
    acceptance=stored_step(service,wid,value,'accept_citation',lambda:mutation(w,revision_id=value['revision_id'],
        evidence_digest=w['pass_revision']['review_evidence_digest'],evidence_refs=[b['ref'] for b in frozen['basis']]))
    service.accept_pass_revision(acceptance)
    value['phase']='downstream';save(service.vault,wid,value)
    return {'ok':True,'phase':'downstream','resumed':False,'delta':delta}

def recover(adapter,request,*,dry_run=False):
    service=adapter.workflow;wid=request['workflow_id']
    if request.get('target_contract_sha256')!=contract_hash():fail('amendment implementation contract changed')
    if request['actor']!=service.status(wid)['actor']:fail('amendment actor differs')
    base=root(service.vault,wid)
    with _exclusive_lock(base/'operator.lock'):
        replay(service.vault,wid)
        action=request.get('action')
        value=state(service.vault,wid)
        if action=='prepare':
            if value and value['operation_id']==request['recovery_id']:
                if plan(service.vault,value)['request']!=request:fail('operation ID already binds a different plan')
                return {'ok':True,'replayed':True,'phase':value['phase'],'resumed':False}
            return prepare(adapter,request,dry_run)
        if not value or value['operation_id']!=request['recovery_id']:fail('continue the same durable amendment operation')
        if dry_run:return {'ok':True,'phase':value['phase'],'applied':False,'resumed':False}
        action_key=action if action!='authorize-call' else action+':'+fingerprint({k:request.get(k) for k in ('logical_work','supersedes_attempt_sha256')})
        previous=value.get('action_requests',{}).get(action_key)
        if previous!=request:
            service._mutation(request)
            ignored={'expected_revision','input_digest','target_contract_sha256'}
            if previous and {k:v for k,v in previous.items() if k not in ignored}!={k:v for k,v in request.items() if k not in ignored}:
                fail('same durable action cannot change its authority or evidence')
            value.setdefault('action_requests',{})[action_key]=dict(request);save(service.vault,wid,value)
        if action=='start':return start(adapter,request)
        if action=='accept-successor':return accept_successor(adapter,request)
        if action=='authorize-call':
            w=service.status(wid);drained(adapter,w)
            if value['phase'] not in ('citation','downstream'):fail('only held native work can receive another bounded reservation')
            logical=request.get('logical_work','');ledger=pending_calls(service.vault,wid)
            attempts=ledger['jobs'].get(logical,{}).get('attempts',[])
            if not attempts or attempts[-1]['operation_id']!=value['operation_id']:
                fail('pin an existing logical reservation in this amendment')
            old=attempts[-1]
            if request.get('supersedes_attempt_sha256')!=fingerprint(old):fail('pin the exact failed/unknown reservation')
            snap=adapter.kanban.task_snapshot(w['kanban']['board_id'],old['binding']['task_id'])
            if snap['task'].get('worker_pid') or not snap['runs'] or any(not r.get('ended_at') for r in snap['runs']):
                fail('old native executor must be ended before controlled regeneration')
            frozen=plan(service.vault,value);basis=request.get('basis',[])
            prior_grants=value.get('retry_grants',{}).get(logical,[])
            if not basis or not request.get('reason','').strip() or basis==frozen['basis'] or any(basis==g['basis'] for g in prior_grants):
                fail('a different corrected cause/new evidence is required; another ID is insufficient')
            for b in basis:
                if hashlib.sha256(_vault_path(service.vault,b['ref']).read_bytes()).hexdigest()!=b['sha256']:fail('retry cause changed')
            phase=logical.partition(':')[0]
            if sum(a['phase']==phase and a['lineage']==frozen['selection']['task_id'] for j in ledger['jobs'].values() for a in j['attempts'])>=frozen['cumulative_limits'][phase]:
                fail('cumulative lineage quota exhausted')
            grant={'supersedes_attempt_sha256':fingerprint(old),'basis':basis,'reason':request['reason'],'additional_calls':1}
            if prior_grants and prior_grants[-1]['supersedes_attempt_sha256']==fingerprint(old):
                if prior_grants[-1]!=grant:fail('an outstanding reservation grant already owns this predecessor')
                return {'ok':True,'replayed':True,'resumed':False}
            value.setdefault('retry_grants',{}).setdefault(logical,[]).append(grant);save(service.vault,wid,value)
            return {'ok':True,'additional_calls':1,'resumed':False,'old_unknown_status_preserved':True}
        if action=='admit':
            from citation_amendment_admission import admit
            return admit(adapter,request)
        if action=='finish':
            w=service.status(wid)
            from hermes_source_units.reduce_coverage import active
            scope=active(service.vault,w)
            if (value['phase']!='awaiting_acceptance' or service.pending_pause(w)!='checkpoint_1'
                    or not scope or scope['ref']['manifest_sha256']!=value['manifest_sha256']):
                fail('finish only after the new exact subset is validated and held at checkpoint_1')
            service.knowledge.validate_batch(w['batch_id'])
            value['phase']='complete';save(service.vault,wid,value)
            return {'ok':True,'phase':'complete','pause':'checkpoint_1','resumed':False}
        fail('unsupported citation amendment action')
