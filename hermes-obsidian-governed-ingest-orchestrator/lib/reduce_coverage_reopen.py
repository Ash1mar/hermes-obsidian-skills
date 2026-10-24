"""Supported pre-Build withdrawal of a limited checkpoint, with replayable writes.

Retired domain records and native outputs remain immutable. This grants no
semantic call allowance and never records checkpoint continuation.
"""
import copy
import hashlib
import json
from hermes_source_units.reduce_coverage import active,pinned,fail,REOPEN
from hermes_source_units.source_units import _exclusive_lock,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint


def pending_path(service,wid):
    return _vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/coverage-reopen.pending.json')


def recover(adapter,request,*,dry_run=False):
    service=adapter.workflow;wid=request['workflow_id'];rid=request['recovery_id']
    lock=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock')
    from native_stop_inventory import verify_cancel_drain
    with _exclusive_lock(lock),_exclusive_lock(service._lock(wid)):
        value=service.status(wid)
        if request['actor']!=value['actor']:fail('reopen actor differs')
        old=next((p for p in value.get('execution_recovery_history',[]) if p['recovery_id']==rid),None)
        if old and not pending_path(service,wid).exists():
            if old['input_digest']!=request['input_digest']:fail('recovery ID differs')
            return {'ok':True,'replayed':True,'report_ref':old['report_ref'],'resumed':False}
        verify_cancel_drain(adapter,wid,timeout=0)
        if value['state']!='cancelled' or not value['cancel_requested']:fail('cancel and drain before reopening')
        with _exclusive_lock(service.knowledge._batch_lock_path(value['batch_id'])):
            journal=pending_path(service,wid)
            if journal.exists():
                plan=json.loads(journal.read_bytes())
                if plan['request']!=request:fail('finish the exact pending recovery first')
            else:
                service._mutation(request);service.assert_execution_ready(value)
                scope=active(service.vault,value)
                if (not scope or scope['ref']!=request.get('coverage_scope')
                        or value['current_stage']!='checkpoint_1'
                        or service.pending_pause(value)!='checkpoint_1'
                        or any(p['boundary']=='checkpoint_1' for p in value['pause_control']['history'])
                        or value.get('release_id')):fail('reopen requires an unreleased limited checkpoint before Build')
                basis=request.get('basis',[])
                if not basis or len(basis)>8:fail('pin new evidence or a corrected cause before reopening')
                for b in basis:pinned(service.vault,b['ref'],b['sha256'])
                batch=service.knowledge._batch(value['batch_id'])
                if not batch['cancel_requested'] or batch['state']!='cancelled':fail('batch must be cancelled')
                if any(s['lease']['worker_id'] or s['state']=='leased' for s in service.knowledge._slices(batch['batch_id'])):fail('live lease blocks reopen')
                if not batch['run_ids'] or not batch.get('global_reduction_id'):fail('limited result is missing')
                for run_id in batch['run_ids']:
                    run=json.loads(service.knowledge._run_path(run_id).read_bytes())
                    if run['state']!='draft' or run.get('coverage_scope')!=scope['ref']:fail('Build consumption closes this recovery')
                coordination=service.knowledge._global_reduction(batch['batch_id'],batch['global_reduction_id'])
                if coordination.get('coverage_scope')!=scope['ref']:fail('global scope differs')
                if any(s in ('ready','running','triage','todo') for s in adapter.kanban.task_states(value['kanban']['board_id']).values()):fail('board must be nondispatchable')
                entries=[]
                for name in ('global-reduce','checkpoint-1-validate'):
                    cards=[c for c in value['kanban']['task_map'] if c['node']==name]
                    if len(cards)!=1:fail('current native cards are required for reopen')
                    card=cards[0];snap=adapter.kanban.task_snapshot(value['kanban']['board_id'],card['task_id'])
                    if snap['task'].get('worker_pid') or any(not r.get('ended_at') for r in snap['runs']):fail('old native executor is live')
                    entries.append({'node':name,'previous_task_id':card['task_id'],
                        'ended_run_ids':[str(r['id']) for r in snap['runs']], 'new_model_calls_granted':0})
                evidence={}
                refs=[value['pause_control']['report_ref'],value['checkpoints']['checkpoint_1'].get('decision_ref')]
                from hermes_source_units.reduce_coverage import checkpoint_ref
                validation_ref=checkpoint_ref(value,'reports','checkpoint-1-validation')
                if _vault_path(service.vault,validation_ref).exists():refs.append(validation_ref)
                for ref in refs:
                    if ref:
                        raw=_vault_path(service.vault,ref).read_bytes()
                        evidence[ref]={'sha256':hashlib.sha256(raw).hexdigest(),'content':raw.decode('utf-8')}
                report={'request':dict(request),'entries':entries,'batch_id':batch['batch_id'],
                    'retired_run_ids':batch['run_ids'],'retired_coordination_id':batch['global_reduction_id'],
                    'previous_pause':copy.deepcopy(value['pause_control']), 'retired_checkpoint_evidence':evidence,
                    'domain_inputs_changed':False,'semantic_retry_authorized':False,'budgets_reset':False}
                ref=f'_system/ledgers/ingest-workflows/{wid}/execution-recoveries/{rid}.json'
                data=_json_bytes(report);new=copy.deepcopy(value);newbatch=copy.deepcopy(batch)
                new.setdefault('execution_recovery_history',[]).append({'recovery_id':rid,'input_digest':request['input_digest'],
                    'report_ref':ref,'report_sha256':hashlib.sha256(data).hexdigest(),'nodes':[e['node'] for e in entries]})
                new.update(current_stage='reducing',resume_stage='reducing',revision=value['revision']+1)
                new['pause_control']={'boundary':None,'evidence_digest':None,'report_ref':None,'history':value['pause_control']['history']}
                new['checkpoints']['checkpoint_1']={'state':'pending','approval_digest':None,'approved_by':None,'decision_ref':None,'decision_sha256':None}
                newbatch.update(run_ids=[],global_reduction_id=None,revision=batch['revision']+1,last_operation='amend-execution')
                writes=[{'ref':ref,'old_sha256':None,'data':data.decode()},
                    {'ref':service.knowledge._batch_path(batch['batch_id']).relative_to(service.vault).as_posix(),
                     'old_sha256':hashlib.sha256(service.knowledge._batch_path(batch['batch_id']).read_bytes()).hexdigest(),'data':_json_bytes(newbatch).decode()},
                    {'ref':service._path(wid).relative_to(service.vault).as_posix(),
                     'old_sha256':hashlib.sha256(service._path(wid).read_bytes()).hexdigest(),'data':_json_bytes(new).decode()}]
                plan={'request':dict(request),'writes':writes,'report_ref':ref}
                from hermes_source_units.validation import validate_record
                validate_record('ingest_workflow',new);validate_record('knowledge_batch',newbatch)
            if dry_run:return {'ok':True,'applied':False,'resumed':False,'new_model_calls_granted':0,'report_ref':plan['report_ref']}
            if not journal.exists():_write_atomic(journal,_json_bytes(plan))
            for item in plan['writes']:
                path=_vault_path(service.vault,item['ref']);raw=item['data'].encode()
                existing=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                if existing==hashlib.sha256(raw).hexdigest():continue
                if existing!=item['old_sha256']:fail('control changed during pending reopen')
                _write_atomic(path,raw)
            journal.unlink()
            return {'ok':True,'applied':True,'resumed':False,'report_ref':plan['report_ref'],'new_model_calls_granted':0}
