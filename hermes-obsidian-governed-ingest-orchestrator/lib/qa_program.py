"""Automatic quality isolation. No operator adjudicates source or model claims."""
import copy,hashlib,json,sqlite3
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.qa_policy import POLICY,enabled,gap
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint

SCOPE='qa-policy/v1'


def isolate_plan(adapter,binding,value,state,packet,error):
    if (not enabled(adapter.workflow.status(binding['workflow_id']))
            or error.code!='SEMANTIC_REVIEW_REQUIRED' or not getattr(error,'semantic_audit_ref',None)):return None
    used={c for p in state['pages'] for c in p['candidate_refs']}|set(state['omitted_candidate_refs'])
    if used.intersection(packet['candidate_refs']):raise ContractError('INCOMPLETE_COVERAGE','$','QA packet overlaps previously assigned evidence')
    ref=gap(adapter.workflow.vault,binding['workflow_id'],binding['node'],
        {'planning_packet':packet['candidate_refs'],'proposal_refs':packet['proposal_refs'],
         'source_input_sha256':fingerprint(value['view'])},
        [{'code':error.code,'reason':str(error),'audit_ref':error.semantic_audit_ref}])
    result=copy.deepcopy(state)
    result['omitted_candidate_refs'].extend(packet['candidate_refs'])
    result['proposal_refs'].extend(packet['proposal_refs'])
    result['reasons'].append('Program isolated an unresolved QA planning packet; '+ref)
    return result


def isolate_pages(adapter,binding,value,state,written,held,planning_failure):
    if not enabled(adapter.workflow.status(binding['workflow_id'])):return None
    if planning_failure:return None  # Planning owns its own packet isolation.
    if any(h.get('code')!='SEMANTIC_REVIEW_REQUIRED' or not h.get('audit_ref') for h in held):return None
    from hermes_source_units.delivery_scope import dependency_partition
    bodies={p['path']:p for p in written};seed={h['path']:{'category':'qa_unresolved','reason':h['reason']} for h in held}
    pages=[{'page_id':p['path'],'path':p['path'],'identity':p['identity'],
        'content':bodies.get(p['path'],{}).get('content','')} for p in state['pages']]
    partition=dependency_partition(pages,seed)
    # QA closure follows actual links to the affected pages. Other unresolved
    # links retain their separate normal validation/implementation contract.
    excluded=set(seed);changed=True
    while changed:
        before=set(excluded)
        excluded.update(pid for pid,deps in partition['dependencies'].items() if excluded.intersection(deps))
        changed=excluded!=before
    kept=[p for p in state['pages'] if p['path'] not in excluded]
    if not kept:raise ContractError('NO_DELIVERABLE_PAGES','$','all pages isolated; zero-page acceptance required, no publication')
    removed=[p for p in state['pages'] if p['path'] in excluded]
    ref=gap(adapter.workflow.vault,binding['workflow_id'],binding['node'],
        {'pages':removed,'candidate_refs':[c for p in removed for c in p['candidate_refs']]},held,
        dependents=sorted(excluded-set(seed)))
    result=copy.deepcopy(state);result['pages']=kept
    result['omitted_candidate_refs'].extend(c for p in removed for c in p['candidate_refs'])
    result['reasons'].append('Program QA isolation; no unsupported page accepted; '+ref)
    return result,ref


def verify_recovery(adapter,w,node,binding,entry,failure,runs):
    """Authenticate current immutable inputs and ended known native responses."""
    from fixed_reduce import preview_snapshot
    from fixed_semantic import system_prompt
    from fixed_semantic_recovery import _request_text
    if not enabled(w) or node.name!='global-reduce' or failure.get('error_code')!='SEMANTIC_REVIEW_REQUIRED':
        raise ContractError('RECOVERY_NOT_ALLOWED','$','only a known global QA hold may use automatic continuation')
    path=_vault_path(adapter.workflow.vault,entry['snapshot_ref']);old=_load_json(path)
    if (hashlib.sha256(path.read_bytes()).hexdigest()!=entry['snapshot_sha256'] or old.get('binding')!=binding
            or fingerprint({k:v for k,v in old.items() if k!='input_id'})!=old['input_id']):
        raise ContractError('STALE_INPUT','$','original QA snapshot changed')
    current=preview_snapshot(adapter,binding)
    if any(old[k]!=current[k] for k in ('contract','view','backend')):raise ContractError('STALE_INPUT','$','QA continuation cannot change materials')
    root=path.parent.parent;calls=[];ended={str(r['id']) for r in runs}
    with sqlite3.connect('file:'+str(Path.home()/'.hermes/state.db')+'?mode=ro',uri=True) as db:
        for p in (root/'semantic-calls').glob('*.json'):
            call=_load_json(p)
            if call.get('native_task_id')!=binding['task_id'] or call.get('reused_from_audit'):continue
            phase=call['phase'];sid=call['model']['session_id']
            if phase not in ('global-plan','global-detail','page-write') or str(call.get('native_run_id')) not in ended:
                raise ContractError('RECOVERY_NOT_ALLOWED','$','unrecognized QA native call')
            rows=list(db.execute('select role,content,tool_calls from messages where session_id=?',(sid,)))
            users=[c for r,c,_ in rows if r=='user' and fingerprint(c)==call['user_sha256']]
            body=json.loads(_request_text(users[0])) if len(users)==1 else {}
            if (db.execute('select source from sessions where id=?',(sid,)).fetchone()!=('kanban',)
                    or len(users)!=1 or any(tc and tc!='[]' for _,_,tc in rows)
                    or not any(r=='assistant' and fingerprint(c)==call['response_sha256'] for r,c,_ in rows)
                    or body.get('phase')!=phase
                    or fingerprint({'system':system_prompt(phase,body['input'],body.get('correction')),'user':users[0]})!=call['input_sha256']):
                raise ContractError('RECOVERY_NOT_ALLOWED','$','QA native request/response proof differs')
            calls.append({'ref':p.relative_to(adapter.workflow.vault).as_posix(),
                'semantic_sha256':fingerprint({k:v for k,v in call.items() if k not in ('receipt_refs','validated')})})
    if not calls:raise ContractError('RECOVERY_NOT_ALLOWED','$','no ended returned native QA evidence')
    for p in (root/'output-attempts').glob('*.json'):
        for a in _load_json(p).get('attempts',[]):
            if a.get('task_id')==binding['task_id'] and a.get('state')=='reserved' and not a.get('audit_ref'):
                raise ContractError('RECOVERY_NOT_ALLOWED','$','unknown completion is not a QA failure')
    return {'qa_policy':{'contract':SCOPE,'policy':POLICY,'snapshot_ref':entry['snapshot_ref'],
        'snapshot_sha256':entry['snapshot_sha256'],'native_calls':calls,'domain_inputs_preserved':True,
        'original_attempts_preserved':True,'new_call_budget_granted':0,'unknown_completion_allowed':False}}


def recover_global(adapter,wid,*,resume=True):
    from automatic_pass_recovery import request
    from native_ingest import control_workflow
    from native_stop_inventory import verify_cancel_drain
    from hermes_source_units.source_units import _exclusive_lock
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{wid}'
    with _exclusive_lock(root/'automatic-qa/.controller.lock'):
        w=adapter.workflow.status(wid)
        if not enabled(w) or w['current_stage']!='reducing' or adapter.workflow.pending_pause(w):return False
        card=next((c for c in w['kanban']['task_map'] if c['node']=='global-reduce'),None)
        fp=root/f'reports/failed-{fingerprint("global-reduce")[:16]}.json'
        if not card or not fp.exists():return False
        failure=_load_json(fp)
        if failure.get('task_id')!=card['task_id'] or failure.get('error_code')!='SEMANTIC_REVIEW_REQUIRED':return False
        candidates=[p for p in (root/'semantic-inputs').glob('*.json')
            if (v:=_load_json(p)).get('contract')=='fixed-reduce-input/v1' and v.get('binding',{}).get('task_id')==card['task_id']]
        if len(candidates)!=1:raise ContractError('RECOVERY_NOT_ALLOWED','$','QA recovery needs one exact source snapshot')
        snapshot=candidates[0];op='auto-qa-'+fingerprint({'node':card['node'],'task':card['task_id']})[:24]
        if not w['cancel_requested']:control_workflow(adapter.workflow.vault,wid,'cancel',op+'-stop')
        verify_cancel_drain(adapter,wid,timeout=0)
        rp=root/f'automatic-qa/{op}.request.json'
        if not rp.exists():_write_atomic(rp,_json_bytes(request(adapter,wid,recovery_id=op,recovery_scope=SCOPE,
            reason='Automatic all-stage QA policy; reuse native bodies, isolate blocked work and actual dependents; no human review',
            entries=[{'node':card['node'],'task_id':card['task_id'],'failure_sha256':hashlib.sha256(fp.read_bytes()).hexdigest(),
                'snapshot_ref':snapshot.relative_to(adapter.workflow.vault).as_posix(),'snapshot_sha256':hashlib.sha256(snapshot.read_bytes()).hexdigest()}])))
        result=adapter.recover_reduce_execution(_load_json(rp))
        _write_atomic(root/f'automatic-qa/{op}.json',_json_bytes(result))
        if resume:control_workflow(adapter.workflow.vault,wid,'resume',op+'-resume')
        return True
