"""Supersede stale source replay grants only inside a certified local amendment."""
import hashlib
from hermes_source_units.citation_amendment import state,plan,fail,pending_calls
from hermes_source_units.source_units import _vault_path
from hermes_source_units.validation import fingerprint

def source_job_supersedes_legacy_framing(adapter,binding):
    """A certified local source job owns its exact wire, not old retry prose."""
    value=state(adapter.workflow.vault,binding['workflow_id'])
    if (not value or value['phase']!='downstream' or binding['node'] not in value.get('downstream_nodes',[])
            or not binding['node'].startswith('resource-reduce:')):return False
    frozen=plan(adapter.workflow.vault,value)
    from citation_amendment_calls import JOB
    return JOB.get()=='source:'+frozen['selection']['task_id']+':'+binding['node'].partition(':')[2]

def source_rebind(adapter,workflow,binding,snapshot,entry):
    value=state(adapter.workflow.vault,workflow['workflow_id'])
    if not value or value['phase'] not in ('downstream','awaiting_acceptance') or binding['node'] not in value.get('downstream_nodes',[]):return False
    frozen=plan(adapter.workflow.vault,value);resource=snapshot['backend']['resource_id']
    old=next((s for s in frozen['sources'] if s['resource_id']==resource and s['reduction_id'] in frozen['retired_source_ids']),None)
    if not old or workflow.get('pass_revision',{}).get('state')!='accepted':fail('source replay supersession lacks its accepted local citation')
    selection=frozen['selection'];successor=value['successor']
    current=adapter.workflow.knowledge._pass_index([selection['task_id']])[1][selection['task_id']]
    if max(current,key=lambda p:p['sequence'])!=successor:fail('accepted citation successor changed')
    refs=[r for p in old['proposals'] for r in p['candidate_refs']]+old['omitted_candidate_refs']
    expected={(successor['pass_id'] if r['pass_id']==selection['pass_id'] else r['pass_id'],r['candidate_id']) for r in refs}
    actual={(r['pass_id'],r['candidate_id']) for refs in snapshot['backend']['mapping'].values() for r in refs}
    if expected!=actual:fail('source citation membership changed beyond the pinned successor mapping')
    from source_hold_recovery import original
    previous=original(adapter.workflow.vault,entry)
    from fixed_semantic_recovery import task_content
    def originals(v):
        packet=v['view']['original_evidence']
        return sorted(task_content(packet,t) for t in packet['tasks'])
    if originals(previous)!=originals(snapshot):fail('source originals/QA/images changed beyond the authorized citation revision')
    before={t['task_id']:h for t,h in zip(previous['backend']['tasks'],previous['backend']['task_hashes'])}
    after={t['task_id']:h for t,h in zip(snapshot['backend']['tasks'],snapshot['backend']['task_hashes'])}
    if set(before)!=set(after) or any(before[t]!=after[t] for t in before if t!=selection['task_id']):
        fail('unrelated source task or exclusion changed')
    snapshot['backend']['superseded_source_replay']={'operation_id':value['operation_id'],
        'plan_sha256':value['plan_sha256'],'old_snapshot_ref':entry['snapshot_ref'],
        'old_snapshot_sha256':entry['snapshot_sha256'],'successor_pass_id':successor['pass_id']}
    return True

def verify_execution_recovery(adapter,workflow,request,binding):
    value=state(adapter.workflow.vault,workflow['workflow_id'])
    if (not value or value['phase']!='downstream' or request.get('amendment_id')!=value['operation_id']
            or binding['node'] not in value.get('downstream_nodes',[]) or not request.get('basis')):
        fail('mechanical recovery is outside the pinned local downstream amendment')
    from citation_amendment_control import contract_hash
    if request.get('target_contract_sha256')!=contract_hash():fail('local mechanical recovery contract changed')
    frozen=plan(adapter.workflow.vault,value)
    for b in request['basis']:
        if hashlib.sha256(_vault_path(adapter.workflow.vault,b['ref']).read_bytes()).hexdigest()!=b['sha256']:fail('corrected local cause changed')
    from citation_amendment_calls import authenticate
    if len(authenticate(adapter,workflow['workflow_id'],'citation'))!=1:fail('accepted native citation evidence is absent')
    from citation_amendment_calls import execution_admission
    admission=execution_admission(adapter,workflow['workflow_id'],binding['node'])
    import fixed_reduce
    current=fixed_reduce.preview_snapshot(adapter,binding)
    return {'local_amendment_execution':{'operation_id':value['operation_id'],'current_input_sha256':current['input_id'],
        'successor_pass_id':value['successor']['pass_id'],'same_lineage_limits':frozen['cumulative_limits'],
        'original_reservation_count':sum(len(j['attempts']) for j in pending_calls(adapter.workflow.vault,workflow['workflow_id'])['jobs'].values()),
        'new_model_calls_granted':0,'unknowns_preserved':True,'reservation_admission':admission}}
