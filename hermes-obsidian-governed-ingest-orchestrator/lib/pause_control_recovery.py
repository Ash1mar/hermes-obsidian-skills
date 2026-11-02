"""Operator-only paused-control recovery. No scheduler, workers or domain calls."""
import copy,hashlib,re
from pathlib import Path
from hermes_source_units import ContractError,mutation_digest
from hermes_source_units.source_units import _load_json,_json_bytes,_write_atomic,_exclusive_lock,_vault_path
from hermes_source_units.partial_finalize import pin,pinned
from hermes_source_units.validation import fingerprint,validate_record

CONTRACT='pause-control-recovery/v1'
def fail(message):raise ContractError('PAUSE_RECOVERY_DENIED','$',message)

def controls(adapter,w):
    """Find supported exact adjacent batch resume/cancel receipts, never guess."""
    vault=adapter.workflow.vault;batch=adapter.workflow.knowledge._batch(w['batch_id'])
    root=vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}/operator-operations"
    rows=[]
    for path in root.glob('*/result.json'):
        r=_load_json(path);s=r.get('status',{})
        if not r.get('ok') or s.get('workflow_id')!=w['workflow_id'] or s.get('batch_id')!=w['batch_id']:continue
        qpath=_vault_path(vault,r['request_ref']);q=_load_json(qpath);req=q['request']
        if (qpath!=path.parent/'request.json' or r['operation_id']!=path.parent.name
                or q['intent']!={'workflow_id':w['workflow_id'],'action':r['action'],'operation_id':r['operation_id']}
                or req['actor']!=w['actor'] or req['workflow_id']!=w['workflow_id']
                or mutation_digest(req)!=req['input_digest']):fail('invalid native control receipt binding')
        rows.append((path,r,req))
    cancels=[row for row in rows if row[1]['action']=='cancel' and row[1]['status']['revision']==w['revision']
        and row[2]['expected_revision']+1==w['revision'] and row[1]['status']['batch_revision']==batch['revision']
        and row[1]['status']['pause']['report_ref']==w['pause_control']['report_ref']
        and row[1]['status']['pause']['evidence_digest']==w['pause_control']['evidence_digest']]
    resumes=[row for row in rows if row[1]['action']=='resume' and row[1]['status']['batch_revision']==batch['revision']-1
        and row[1]['status']['batch_state']!='cancelled']
    if len(cancels)!=1 or len(resumes)!=1:fail('one exact supported cancel and preceding batch resume required')
    c,r=cancels[0],resumes[0]
    if not r[1]['status']['revision']<c[2]['expected_revision']:fail('control receipt sequence differs')
    return {'cancel':pin(vault,c[0].relative_to(vault).as_posix()),
        'resume':pin(vault,r[0].relative_to(vault).as_posix()),
        'cancel_request':pin(vault,c[1]['request_ref']),'resume_request':pin(vault,r[1]['request_ref'])},r[1]['status']

def recover(adapter,request,*,dry_run=False):
    from native_ingest import operator_only
    from native_stop_inventory import verify_cancel_drain,verify_pause_drain
    operator_only();s=adapter.workflow;s._check_request(request);wid=request['workflow_id'];vault=s.vault
    op=request.get('operation_id','')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',op) or request.get('action')!='recover-paused-control' or not request.get('reason'):fail('explicit bound recovery required')
    root=vault/f'_system/ledgers/ingest-workflows/{wid}/pause-control-recoveries/{op}'
    with _exclusive_lock(vault/f'_system/ledgers/ingest-workflows/.dispatch-locks/{wid}.lock'),_exclusive_lock(s._lock(wid)):
        w=s.status(wid);k=s.knowledge
        with _exclusive_lock(k._batch_lock_path(w['batch_id'])):
            plan_path=root/'plan.json';receipt=root/'result.json'
            if plan_path.exists():
                plan=_load_json(plan_path)
                if plan['request']!=request:fail('operation identity already bound')
                for p in [plan['predecessor'],*plan['controls'].values()]:pinned(vault,p)
                if receipt.exists():
                    if w!=plan['after_workflow'] or k._batch(w['batch_id'])!=plan['after_batch']:fail('replay state has changed')
                    verify_pause_drain(adapter,wid,timeout=0)
                    return {**_load_json(receipt),'replayed':True}
            else:
                if w['actor']!=request['actor'] or w['revision']!=request['expected_revision']:fail('workflow actor/revision changed')
                if w['state']!='cancelled' or not w['cancel_requested'] or not w['pause_control']['boundary']:fail('cancelled latched pause required')
                verify_cancel_drain(adapter,wid,timeout=0)
                pointer=request['pause_report'];old=pinned(vault,pointer)
                if pointer['ref']!=w['pause_control']['report_ref'] or old['evidence_digest']!=w['pause_control']['evidence_digest'] or old['evidence_digest']!='sha256:'+fingerprint(old['evidence']):fail('original pause report differs')
                control,prior=controls(adapter,w);batch=k._batch(w['batch_id'])
                if batch['state']!='cancelled' or not batch['cancel_requested'] or batch['last_operation']!='cancel':fail('batch is not a supported cancellation')
                previous=copy.deepcopy(batch)
                previous.update(revision=prior['batch_revision'],state=prior['batch_state'],cancel_requested=False,last_operation='slice')
                bref=k._batch_path(w['batch_id']).relative_to(vault).as_posix()
                if hashlib.sha256(_json_bytes(previous)).hexdigest()!=old['evidence']['files'].get(bref):fail('supported pre-cancel batch differs from pause hash')
                after=copy.deepcopy(previous);after['revision']=batch['revision']+1
                nw=copy.deepcopy(w);nw.update(state=w['resume_stage'],resume_stage=None,cancel_requested=False,revision=w['revision']+1)
                if nw['state']!=w['current_stage'] or nw['current_stage']!=old['evidence']['stage']:fail('same stage must be retained')
                # Fresh normal evidence validation against a read-only batch view.
                # Only its pinned control ledger bytes are replaced explicitly.
                ks=copy.copy(k);original_batch=k._batch
                ks._batch=lambda bid:after if bid==w['batch_id'] else original_batch(bid)
                ss=copy.copy(s);ss.knowledge=ks
                evidence=ss._pause_evidence(nw,w['pause_control']['boundary'])
                compare=copy.deepcopy(evidence);compare['files'][bref]=old['evidence']['files'][bref]
                if compare!=old['evidence']:fail('content, inputs, coverage, scope or dependency evidence changed')
                evidence['files'][bref]=hashlib.sha256(_json_bytes(after)).hexdigest()
                report={'contract':'hermes-ingest-pause/v1','evidence':evidence,'evidence_digest':'sha256:'+fingerprint(evidence)}
                nw['pause_control']={**w['pause_control'],'report_ref':(root/'pause.json').relative_to(vault).as_posix(),'evidence_digest':report['evidence_digest']}
                validate_record('knowledge_batch',after);validate_record('ingest_workflow',nw)
                plan={'contract':CONTRACT,'request':request,'predecessor':pointer,'controls':control,
                    'before_workflow':w,'before_batch':batch,'after_workflow':nw,'after_batch':after,'pause':report,
                    'new_domain_calls':0,'downstream_dispatched':False}
                if dry_run:return {'ok':True,'dry_run':True,'boundary':w['pause_control']['boundary'],'new_domain_calls':0}
                _write_atomic(plan_path,_json_bytes(plan))
            # Durable intent precedes both ledgers; exact interrupted states alone
            # may finish the same operation. No revision rollback or overwrite.
            if w not in (plan['before_workflow'],plan['after_workflow']) or k._batch(w['batch_id']) not in (plan['before_batch'],plan['after_batch']):fail('interrupted recovery state differs')
            ks=copy.copy(k);original_batch=k._batch
            ks._batch=lambda bid:plan['after_batch'] if bid==w['batch_id'] else original_batch(bid)
            ss=copy.copy(s);ss.knowledge=ks
            fresh=ss._pause_evidence(plan['after_workflow'],plan['after_workflow']['pause_control']['boundary'])
            bref=k._batch_path(w['batch_id']).relative_to(vault).as_posix()
            fresh['files'][bref]=hashlib.sha256(_json_bytes(plan['after_batch'])).hexdigest()
            if fresh!=plan['pause']['evidence']:fail('evidence changed before completing recovery')
            report={**plan['pause'],'recovery':pin(vault,plan_path.relative_to(vault).as_posix())}
            _write_atomic(root/'pause.json',_json_bytes(report))
            _write_atomic(k._batch_path(w['batch_id']),_json_bytes(plan['after_batch']))
            s._write(plan['after_workflow'])
            verify_pause_drain(adapter,wid,timeout=0)
            result={'ok':True,'contract':CONTRACT,'revision':plan['after_workflow']['revision'],
                'boundary':plan['after_workflow']['pause_control']['boundary'],'new_domain_calls':0,
                'pause_report':pin(vault,(root/'pause.json').relative_to(vault).as_posix()),'predecessor':plan['predecessor'],
                'downstream_dispatched':False,'resumed':False}
            _write_atomic(receipt,_json_bytes(result));return result
