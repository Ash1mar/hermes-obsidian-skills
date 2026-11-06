"""Trusted test-only node reentry. No new Vault/workflow or domain products."""
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from hermes_source_units import FileIngestWorkflowService
from hermes_source_units.source_units import _load_json, _json_bytes, _write_atomic, _vault_path, _exclusive_lock
from hermes_source_units.validation import fingerprint, validate_record
from hermes_source_units.workflow_test import CONTRACT, NODES, POLICY, root, manifest, inventory, fail


def digest(raw):return hashlib.sha256(raw).hexdigest()


def rank(kind):
    return NODES.index('pass' if kind=='pass-slice' else kind) if kind in NODES or kind=='pass-slice' else len(NODES)


def prefix_cards(w, boundary, files):
    """Only completed prefix Pass slices keep their original native identity."""
    result={}
    for c in w['kanban']['task_map']:
        kind=c['node'].partition(':')[0]
        keep=rank(kind)<rank(boundary['node'])
        if kind=='pass-slice':
            ref=f'_system/ledgers/knowledge-build-batches/{w["batch_id"]}/slices/{c["node"].partition(":")[2]}.json'
            keep=(rank(boundary['node'])>=rank('pass') and ref in files
                  and json.loads(files[ref])['state']=='completed')
        if keep:result[c['node']]=c['idempotency_key'].split(':sha256:')[-1]
    return {k:'sha256:'+v for k,v in result.items()}


def _read_boundary(vault,wid,node):
    cache={};files={}
    boundary,pin=manifest(vault,wid,node,payloads=cache)
    for ref,h in boundary['files'].items():
        files[ref]=cache[h]
    return boundary,pin,files


def authenticate_prefix(adapter,boundary,files,prefix):
    """Read-only snapshot materialization, not a new executable workflow."""
    from fixed_semantic_evidence import pass_evidence
    seals=boundary.get('native_prefix',{})
    for c in json.loads(files[f'_system/ledgers/ingest-workflows/{boundary["workflow_id"]}.json'])['kanban']['task_map']:
        if c['node'] not in prefix:continue
        saved=seals.get(c['task_id'])
        if not saved:fail('prefix lacks a stopped native ownership seal')
        live=adapter.kanban.task_snapshot(saved['board_id'],c['task_id'])
        task=live['task'];old=saved['snapshot']['task']
        if (task.get('worker_pid') or task.get('current_run_id') or any(not r.get('ended_at') for r in live.get('runs',[]))
                or any(task.get(k)!=old.get(k) for k in ('id','body','created_by','idempotency_key'))
                or not {str(r['id']) for r in saved['snapshot'].get('runs',[])}<={str(r['id']) for r in live.get('runs',[])}):
            fail('prefix native owner or completion changed')
    # Existing reader validates genuine native Citation/preflight receipts against
    # the exact captured bytes. No model, adapter sync, or domain write is called.
    proofs=[]
    with tempfile.TemporaryDirectory(prefix='hermes-node-test-read-') as folder:
        view=Path(folder)
        for ref,raw in files.items():_write_atomic(_vault_path(view,ref),raw)
        service=FileIngestWorkflowService(view)
        w=service.status(boundary['workflow_id'])
        from types import SimpleNamespace
        reader=SimpleNamespace(workflow=service)
        for c in w['kanban']['task_map']:
            if c['node'] in prefix and c['node'].startswith('pass-slice:'):
                proof=pass_evidence(Path.home()/'.hermes',reader,w,c['task_id'])
                if proof is None:fail('unsupported native Pass prefix')
                proofs.append(proof)
    return proofs


def describe(adapter,wid,node):
    w=adapter.workflow.status(wid)
    vault=adapter.workflow.vault;wid=w['workflow_id']
    boundary,pin,files=_read_boundary(vault,wid,node)
    captured=json.loads(files[f'_system/ledgers/ingest-workflows/{wid}.json'])
    validate_record('ingest_workflow',captured)
    if (captured['workflow_id']!=wid or captured['actor']!=w['actor']
            or captured['start_digest']!=w['start_digest'] or boundary['actor']!=w['actor']
            or boundary['logical_vault_id']!=_load_json(vault/'_system/vault.json')['vault']['id']):
        fail('boundary does not belong to the same workflow identity')
    current=inventory(vault)
    raw={ref:h for ref,(h,_) in current.items() if ref.startswith('10_Raw/')}
    if raw!=boundary['raw_sources']:fail('source originals changed; a test restart cannot disguise new inputs')
    prefix=prefix_cards(captured,boundary,files)
    proofs=authenticate_prefix(adapter,boundary,files,prefix)
    return {'boundary':boundary,'boundary_sha256':pin,'files':files,'current':current,
            'workflow':captured,'prefix_fingerprints':prefix,'native_pass_proofs':proofs}


def _apply(vault,journal,plan):
    retired=journal.parent/'retired'
    for ref,h in plan['retired_files'].items():
        source=_vault_path(vault,ref);target=_vault_path(retired,ref)
        if target.exists():
            if digest(target.read_bytes())!=h:fail('retired evidence changed')
            if source.exists():
                raw=source.read_bytes()
                if digest(raw)==h:source.unlink()
                elif ref not in plan['writes']:fail('unexpected live data during retirement')
            continue
        if source.is_symlink() or digest(source.read_bytes())!=h:fail('data changed before retirement')
        target.parent.mkdir(parents=True,exist_ok=True);os.replace(source,target)
    blobs=root(vault,plan['request']['workflow_id'])/'blobs'
    cache={}
    for ref,item in plan['writes'].items():
        if 'inline' in item:raw=item['inline'].encode()
        else:
            if item['blob'] not in cache:cache[item['blob']]=(blobs/item['blob']).read_bytes()
            raw=cache[item['blob']]
        if digest(raw)!=item['sha256']:fail('restore payload changed')
        p=_vault_path(vault,ref)
        if p.exists() and p.read_bytes()!=raw:fail('restore target changed')
        if not p.exists():_write_atomic(p,raw)
    for ref,h in plan['preserved_files'].items():
        if digest(_vault_path(vault,ref).read_bytes())!=h:fail('valid prefix changed')
    plan['phase']='admitted';_write_atomic(journal,_json_bytes(plan))


def _seal(journal,plan):
    row={'contract':CONTRACT,'phase':'admitted','workflow_id':plan['request']['workflow_id'],
         'operation_id':plan['request']['operation_id'],'input_digest':plan['request']['input_digest'],
         'report_ref':plan['report_ref'],'report_sha256':plan['writes'][plan['report_ref']]['sha256']}
    p=journal.with_name('admitted.json')
    if p.exists() and _load_json(p)!=row:fail('admission seal changed')
    if not p.exists():_write_atomic(p,_json_bytes(row))


def reenter(adapter,request,*,dry_run=False):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('trusted test operator required')
    if request.get('test_mode')!=CONTRACT or request.get('overwrite_downstream') is not True:
        fail('explicit same-workflow node retest required')
    op=request.get('operation_id','');node=request.get('from_node')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,70}',op) or node not in NODES:fail('invalid operation or node')
    if type(request.get('pass_capacity')) is not int or not 1<=request['pass_capacity']<=10:fail('invalid pinned Pass capacity')
    service=adapter.workflow;vault=service.vault;wid=request['workflow_id'];service._check_request(request)
    journal=vault/f'_system/ledgers/ingest-workflows/{wid}/workflow-tests/{op}/transaction.json'
    lock=vault/f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock'
    from contextlib import ExitStack
    with _exclusive_lock(lock),_exclusive_lock(service._lock(wid)),ExitStack() as locks:
        if journal.exists():
            plan=_load_json(journal)
            if plan['request']!=request:fail('stable operation inputs changed')
            if not dry_run:
                if plan['phase']!='admitted':_apply(vault,journal,plan)
                _seal(journal,plan)
            return {'ok':True,'applied':False,'replayed':True,'workflow_id':wid,'report_ref':plan['report_ref'],'resumed':False}
        current_w=service._mutation(request)
        if current_w['batch_id']:locks.enter_context(_exclusive_lock(service.knowledge._batch_lock_path(current_w['batch_id'])))
        if (current_w['state'] not in ('partial','completed','cancelled')
                or current_w['scope'].get('pause_after') or current_w['scope'].get('execution_mode')!='auto_full'):
            fail('stop and drain an auto_full test workflow without durable pauses first')
        identity=_load_json(vault/'_system/vault.json')['vault']
        policy=_load_json(vault/'_system/metadata/retrieval-policy.json')
        if 'test' not in identity['security_domain'].lower() or policy.get('query_enabled') is not False:
            fail('test domain and Query disabled required')
        if any(p!=service._path(wid) for p in (vault/'_system/ledgers/ingest-workflows').glob('*.json')):
            fail('test Vault must contain only the selected workflow')
        from native_stop_inventory import workflow_processes
        if workflow_processes(wid,current_w['kanban']['board_id']):fail('native consumers are active')
        if any(s in ('ready','running','triage','todo') for s in adapter.kanban.task_states(current_w['kanban']['board_id']).values()):
            fail('native cards are not drained')
        if current_w['batch_id'] and any(s['lease']['worker_id'] or s['state']=='leased'
                for s in service.knowledge._slices(current_w['batch_id'])):fail('native leases remain')
        data=describe(adapter,wid,node)
        if data['boundary_sha256']!=request.get('boundary_sha256'):fail('selected boundary changed')
        before=data['workflow'];new=copy.deepcopy(before);prefix=data['prefix_fingerprints']
        new.update(state='cancelled',cancel_requested=True,resume_stage=before['current_stage'],revision=current_w['revision']+1)
        new['kanban']['task_map']=[c for c in before['kanban']['task_map'] if c['node'] in prefix]
        late=rank(node)>rank('global-reduce')
        report_ref=journal.with_name('admission.json').relative_to(vault).as_posix()
        report={'contract':CONTRACT,'request':dict(request),
                'boundary_sha256':data['boundary_sha256'],'prefix_fingerprints':prefix,
                'budget_policy':'authorized node retest; prior consumed calls retired for cumulative accounting; operation replay never grants new credit'}
        new['workflow_test']={'operation_id':op,'from_node':node,'report_ref':report_ref,
            'report_sha256':digest(_json_bytes(report)),
            'prefix_fingerprints':[{'node':name,'fingerprint':fp} for name,fp in sorted(prefix.items())],'presentation_reused':late,
            'pass_capacity':request['pass_capacity']}
        files=data['files'];desired=dict(data['boundary']['files']);inline={}
        def set_value(ref,value):
            raw=_json_bytes(value);inline[ref]=raw;desired[ref]=digest(raw)
        set_value(service._path(wid).relative_to(vault).as_posix(),new)
        if new['batch_id']:
            ref=service.knowledge._batch_path(new['batch_id']).relative_to(vault).as_posix()
            batch=json.loads(files[ref]);old_batch=service.knowledge._batch(current_w['batch_id']) if current_w['batch_id'] else batch
            batch.update(state='cancelled',cancel_requested=True,revision=max(batch['revision'],old_batch['revision'])+1,last_operation='cancel')
            if batch['slices_initialized'] and any(json.loads(files[r])['lease']['worker_id'] for r in files
                    if r.startswith(f'_system/ledgers/knowledge-build-batches/{new["batch_id"]}/slices/') and r.endswith('.json')):
                fail('captured boundary contains a live slice lease')
            set_value(ref,batch);validate_record('knowledge_batch',batch)
        if not late:
            desired.pop(f'_system/ledgers/ingest-workflows/{wid}/presentation-contract.json',None)
            new.pop('reduce_test',None);new['page_execution_contract']='unified-page-execution/v3'
            set_value(service._path(wid).relative_to(vault).as_posix(),new)
        set_value(POLICY,{'contract':CONTRACT,'workflow_id':wid,'actor':new['actor'],'capture':True})
        validate_record('ingest_workflow',new)
        # Outside the managed control plane, only registered page outputs may be
        # replaced. Originals and unrelated user notes must stay byte-identical.
        owned={s['current_path'] for s in service.knowledge.list_identities()['subjects']}
        for ref,(h,raw) in data['current'].items():
            if desired.get(ref)!=h and not ref.startswith('_system/') and ref not in owned:
                fail('unmanaged user data changed since boundary: '+ref)
        retired={ref:h for ref,(h,raw) in data['current'].items() if desired.get(ref)!=h}
        preserved={ref:h for ref,(h,raw) in data['current'].items() if desired.get(ref)==h}
        writes={ref:({'inline':inline[ref].decode(),'sha256':h} if ref in inline else {'blob':h,'sha256':h})
                for ref,h in desired.items() if data['current'].get(ref,(None,None))[0]!=h}
        basis_ref=journal.with_name('evidence.json').relative_to(vault).as_posix()
        basis={'boundary':data['boundary'],'retired_files':retired,'native_pass_proofs':data['native_pass_proofs']}
        raw=_json_bytes(basis);writes[basis_ref]={'inline':raw.decode(),'sha256':digest(raw)}
        report.update(evidence_ref=basis_ref,evidence_sha256=digest(raw))
        # Pin the complete report after retirement planning is known.
        new['workflow_test']['report_sha256']=digest(_json_bytes(report))
        raw=_json_bytes(new);wref=service._path(wid).relative_to(vault).as_posix()
        writes[wref]={'inline':raw.decode(),'sha256':digest(raw)}
        raw=_json_bytes(report);writes[report_ref]={'inline':raw.decode(),'sha256':digest(raw)}
        plan={'request':dict(request),'phase':'prepared','report_ref':report_ref,'retired_files':retired,
              'preserved_files':preserved,'writes':writes}
        result={'ok':True,'applied':not dry_run,'workflow_id':wid,'from_node':node,'report_ref':report_ref,
                'prefix_cards_reused':len(prefix),'retired_files':len(retired),'resumed':False}
        if dry_run:return result
        _write_atomic(journal,_json_bytes(plan));_apply(vault,journal,plan);_seal(journal,plan)
        return result


def handoff(adapter,request,result):
    """Journal replay observes an admitted cycle; later stops stay held."""
    from orchestration import dispatch,_mutation
    from watcher_runtime import coordinator_pid,confirm_handoff
    from hermes_source_units.presentation_contract import pin
    from page_protocol import preflight
    service=adapter.workflow;w=service.status(request['workflow_id']);vault=service.vault
    initial=w['state']=='cancelled' and w['cancel_requested'] and w['revision']==request['expected_revision']+1
    if initial:
        if request.get('from_node')=='bootstrap':
            import subprocess,sys
            script=Path(__file__).resolve().parents[2]/'hermes-obsidian-vault-bootstrap/scripts/validate_source_unit_vault.py'
            subprocess.run([sys.executable,str(script),'--vault',str(vault)],check=True,capture_output=True)
        preflight(pin(service,w))
        if w.get('workflow_test') and w['current_stage']=='analyzing' and w['batch_id']:
            from hermes_source_units.pass_capacity import set_capacity
            result['capacity']=set_capacity(adapter,w['workflow_id'],w['workflow_test']['pass_capacity'],
                request['operation_id']+'-capacity')
        result['handoff']=dispatch(vault,'resume',_mutation(w))
        result['watcher']=confirm_handoff(vault,w['workflow_id'],result['handoff'].get('reconciler_pid'))
    elif not w['cancel_requested'] and w['state'] not in ('completed','partial','failed') and not service.pending_pause(w):
        pid=coordinator_pid(vault,w['workflow_id'])
        if not pid:
            result['handoff']=dispatch(vault,'resume',_mutation(w));pid=result['handoff'].get('reconciler_pid')
        result['watcher']=confirm_handoff(vault,w['workflow_id'],pid)
    else:result['workflow']=service.status(w['workflow_id'],compact=True)
    return result
