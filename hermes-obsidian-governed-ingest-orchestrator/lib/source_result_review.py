"""One operator-selected packet review before any global consumption.

All old proposals remain immutable. This authenticates native outcomes; it does
not decide which facts to adopt, omit, rewrite or group.
"""
import copy,hashlib,json,sqlite3
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _load_json,_vault_path
from hermes_source_units.resource_amendment import SCOPE,pending


def fail(message):raise ContractError('RECOVERY_NOT_ALLOWED','$',message)


def native_home():return Path.home()/'.hermes'


def contract_hash():
    import hermes_source_units.knowledge_build as kb
    root=Path(__file__).resolve().parent;canonical=Path(kb.__file__).parent
    paths=[root/n for n in ('source_result_review.py','fixed_reduce.py','fixed_reduce_packets.py','fixed_semantic.py')]
    paths += [canonical/n for n in ('resource_amendment.py','knowledge_build.py','schemas/contracts.json')]
    return hashlib.sha256(b''.join(p.read_bytes() for p in paths)).hexdigest()


def original_view(view):
    return {k:v for k,v in view.items() if k not in ('packet_review','hold_resolution')}


def review_view(value,view):
    grant=value['backend'].get('source_review')
    if grant and fingerprint(view) in grant['selected_packet_sha256']:
        return {**view,'packet_review':{'authorization_sha256':grant['authorization_sha256']}}
    return view


def replay_paths(adapter,binding,view):
    """Index the existing exact replay verifier; never substitute a cached answer."""
    workflow=adapter.workflow.status(binding['workflow_id']);grant=pending(adapter.workflow,workflow,binding['node'])
    if not grant:return None
    key=fingerprint(original_view(view));item=grant['packet_audits'].get(key)
    if not item:fail('source packet differs from its reviewed complete input')
    if 'packet_review' in view:return []
    path=_vault_path(adapter.workflow.vault,item['original_audit_ref'])
    if hashlib.sha256(path.read_bytes()).hexdigest()!=item['original_audit_sha256']:fail('original packet audit changed')
    return [path]


def native(adapter,workflow,binding,path,db,seen=None):
    """Follow genuine replay provenance, then check the original ended session."""
    from revision_disposition import native_user
    from fixed_semantic import decode_semantic_response
    seen=set() if seen is None else seen
    ref=path.relative_to(adapter.workflow.vault).as_posix()
    prefix=f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}/semantic-calls/"
    if not ref.startswith(prefix) or '/' in ref[len(prefix):] or ref in seen:fail('invalid or cyclic native audit reference')
    seen.add(ref);audit=_load_json(path)
    if (audit['workflow_id']!=workflow['workflow_id'] or audit['node']!=binding['node']
            or audit['phase']!='resource-reduce' or audit.get('validated') is False
            or audit['template_hash']!=binding['template_hash']):fail('native packet identity differs')
    if audit.get('reused_from_audit'):
        origin,body,draft,origin_path=native(adapter,workflow,binding,_vault_path(adapter.workflow.vault,audit['reused_from_audit']),db,seen)
        if any(audit.get(k)!=origin.get(k) for k in ('input_sha256','user_sha256','schema_sha256','response_sha256','image_inputs','model')):
            fail('replay does not match its original native call')
        return audit,body,draft,origin_path
    model=audit['model'];session=model['session_id']
    rows=list(db.execute('SELECT role,content,tool_calls FROM messages WHERE session_id=?',(session,)))
    users=[u for role,c,_ in rows if role=='user' and (u:=native_user(c,audit)) is not None]
    answers=[c for role,c,_ in rows if role=='assistant' and fingerprint(c)==audit['response_sha256']]
    if (model.get('origin')!='hermes_native_model' or model.get('response_sha256')!=audit['response_sha256']
            or db.execute('SELECT source FROM sessions WHERE id=?',(session,)).fetchone()!=('kanban',)
            or len(users)!=1 or len(answers)!=1 or any(t and t!='[]' for _,_,t in rows)):
        fail('native transcript or response differs')
    snap=adapter.kanban.task_snapshot(workflow['kanban']['board_id'],audit['native_task_id'])
    task=snap['task'];task_body=json.loads(task.get('body','{}'))
    if (task.get('worker_pid') or task.get('created_by')!='ingest-workflow'
            or task_body.get('node')!=binding['node'] or task_body.get('workflow_id')!=workflow['workflow_id']
            or not any(str(r['id'])==str(audit['native_run_id']) and r.get('ended_at') and r.get('worker_pid') for r in snap['runs'])):
        fail('original native run is not verifiably ended')
    from fixed_semantic_recovery import _request_text
    body,_=json.JSONDecoder().raw_decode(_request_text(users[0]));draft,_=decode_semantic_response(answers[0])
    return audit,body,draft,path


def verify(adapter,workflow,node,binding,entry):
    import fixed_reduce
    import source_hold_recovery as h
    from fixed_reduce_packets import plan,merge,validate_packet
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_schema import schema
    from fixed_semantic_media import image_catalog,select_images
    k=adapter.workflow.knowledge;batch=k._batch(workflow['batch_id'])
    old_id=entry['reduction_id'];path=k._resource_reduction_path(batch['batch_id'],old_id)
    if (old_id not in batch['resource_reduction_ids'] or batch['global_reduction_id'] or batch['run_ids']
            or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['reduction_sha256']):
        fail('review requires an unchanged, active, globally unconsumed source result')
    record=k._resource_reduction(batch['batch_id'],old_id)
    if node.kind!='resource-reduce' or record['resource_id']!=node.name.partition(':')[2]:fail('review crosses source scope')
    old=h.original(adapter.workflow.vault,entry)
    current=fixed_reduce._snapshot(adapter,binding,{'batch_id':batch['batch_id']})
    if old['binding']!=binding or h.source_identity(current)!=h.source_identity(old):fail('source inputs changed before review')
    packets=plan(adapter,old,prepare_request_projection);by_view={fingerprint(p['view']):p for p in packets}
    requested=entry.get('selected_audits',[])
    if not requested or len({r['ref'] for r in requested})!=len(requested):fail('select unique native packets for review')
    selected={r['ref']:r['sha256'] for r in requested};found=set();drafts={};index={}
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}/semantic-calls"
    catalog=image_catalog(adapter,old['view']['original_evidence'])
    with sqlite3.connect('file:'+str(native_home()/'state.db')+'?mode=ro',uri=True) as db:
        for audit_path in root.glob('*.json'):
            a=_load_json(audit_path)
            if a.get('native_task_id')!=binding['task_id'] or a.get('phase')!='resource-reduce' or a.get('validated') is not True:continue
            if not any(_load_json(_vault_path(adapter.workflow.vault,r)).get('reduction_id')==old_id for r in a.get('receipt_refs',[])):continue
            audit,body,draft,origin_path=native(adapter,workflow,binding,audit_path,db)
            base=h.unclarified_view(body['input'],set(by_view));key=fingerprint(base)
            if key not in by_view or key in drafts:fail('native packets do not match the unique complete source plan')
            packet=by_view[key];images=select_images(packet['view']['original_evidence']['tasks'],catalog)
            projected=prepare_request_projection('resource-reduce',body['input'],schema('resource-reduce'),images)
            if (body.get('phase')!='resource-reduce' or fingerprint(projected['wire'])!=audit['input_sha256']
                    or fingerprint(projected['wire']['user'])!=audit['user_sha256'] or fingerprint(schema('resource-reduce'))!=audit['schema_sha256']
                    or projected['input_size']>30000 or [{k:v for k,v in i.items() if k!='url'} for i in images]!=audit.get('image_inputs',[])):
                fail('native wire, schema or full original images differ')
            validate_packet(packet,draft);drafts[key]=draft
            ref=audit_path.relative_to(adapter.workflow.vault).as_posix()
            index[key]={'original_audit_ref':origin_path.relative_to(adapter.workflow.vault).as_posix(),
                        'original_audit_sha256':hashlib.sha256(origin_path.read_bytes()).hexdigest()}
            if ref in selected:
                if hashlib.sha256(audit_path.read_bytes()).hexdigest()!=selected[ref]:fail('selected audit bytes changed')
                found.add(ref);index[key]['selected']=True
                revised=review_view({'backend':{'source_review':{'selected_packet_sha256':[key],'authorization_sha256':'0'*64}}},base)
                if prepare_request_projection('resource-reduce',revised,schema('resource-reduce'),images)['input_size']>30000:
                    fail('complete review packet exceeds the existing budget')
    if found!=set(selected) or set(drafts)!=set(by_view):fail('formal source lacks complete authenticated packet provenance')
    expanded=fixed_reduce.expand(old,merge(old,packets,[drafts[fingerprint(p['view'])] for p in packets]))
    if any(expanded[k]!=record[k] for k in ('proposals','omitted_candidate_refs','reason','tasks')):
        fail('native packet outputs do not reconstruct the active formal source result')
    return {'replaces_reduction_id':old_id,'reduction_sha256':entry['reduction_sha256'],
            'snapshot_ref':entry['snapshot_ref'],'snapshot_sha256':entry['snapshot_sha256'],
            'source_identity':h.source_identity(old),'packet_audits':index,
            'selected_packet_sha256':[key for key,v in index.items() if v.get('selected')],
            'selected_candidate_refs':[r for key,p in by_view.items() if index[key].get('selected')
                for c in p['candidate_refs'] for r in old['backend']['mapping'][c]],
            'unchanged_packet_count':len(index)-len(selected),'parent_passes_changed':False}
