"""Observed Reduce inputs and mechanical expansion of short candidate handles."""
import copy
import hashlib

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint, validate_record
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard
from fixed_semantic_schema import validate


def fail(code, message):
    raise ContractError(code, '$', message)


def snapshot(adapter, binding):
    checked = adapter.worker_check(binding)
    workflow = adapter.workflow.status(binding['workflow_id'])
    service = adapter.workflow.knowledge
    batch = service._batch(checked['batch_id'])
    kind, _, resource = binding['node'].partition(':')
    tasks = [service._task(tid) for tid in batch['task_ids']]
    if kind == 'resource-reduce':
        tasks = [t for t in tasks if {r['unit_ref']['resource_id'] for r in t['target_refs']} == {resource}]
    elif kind != 'global-reduce':
        fail('ACCESS_DENIED','binding is not a reducer')
    if not tasks: fail('INCOMPLETE_COVERAGE','reducer has no owned tasks')
    candidates, passes = service._pass_index(t['task_id'] for t in tasks)
    reductions = [service._resource_reduction(batch['batch_id'], rid)
        for rid in sorted(batch['resource_reduction_ids'])] if kind == 'global-reduce' else []
    proposed = {(r['pass_id'],r['candidate_id']) for reduction in reductions
        for p in reduction['proposals'] for r in p['candidate_refs']}
    eligible = {key:pair for key,pair in candidates.items()
        if pair[0]['pass_kind']=='citation' and (kind=='resource-reduce' or key in proposed)}
    view = {'phase':kind,'candidates':[]}
    mapping, aliases, grouped = {}, {}, {}
    # Deduplicate only identical semantics AND inspection QA, never similar names.
    for key, (record,candidate) in sorted(eligible.items()):
        observation = {k:copy.deepcopy(v) for k,v in candidate.items() if k not in ('candidate_id','support_refs')}
        observation['evidence_qa'] = [{k:v for k,v in i.items() if k!='source_ref'} for i in record['inspections']]
        digest = fingerprint(observation)
        handle = grouped.get(digest)
        if handle is None:
            handle = 'c'+str(len(mapping)+1); grouped[digest]=handle; mapping[handle]=[]
            view['candidates'].append({'candidate':handle,**observation})
        mapping[handle].append({'pass_id':key[0],'candidate_id':key[1]})
        aliases[key]=handle
    backend = {'kind':kind,'batch_id':batch['batch_id'],'actor':workflow['actor'],
        'resource_id':resource,'tasks':[{'task_id':t['task_id'],'revision':t['revision']} for t in tasks],
        'task_hashes':[fingerprint(t) for t in tasks], 'mapping':mapping,
        'passes':[fingerprint(p) for tid in sorted(passes) for p in passes[tid]],
        'document_registry_revision':service._document_registry_revision(),
        'reductions':reductions,'identities':None,'existing_pages':{}}
    if kind=='global-reduce':
        view['proposals'] = [{'proposal':'p'+str(index), 'summary':p['summary'],
            'candidate_refs':list(dict.fromkeys(aliases[(r['pass_id'],r['candidate_id'])] for r in p['candidate_refs'])),
            'identity_hints':p['identity_hints'],'path_hints':p['path_hints']}
            for index,p in enumerate((p for reduction in reductions for p in reduction['proposals']),1)]
        registry = service.list_identities(); backend['identities']=registry
        view['existing_pages']=[]
        for subject in registry['subjects']:
            path = _vault_path(service.vault, subject['current_path'])
            page = _load_json(_vault_path(service.vault,
                f"_system/knowledge-builds/page-revisions/{subject['page_id']}/{subject['current_revision_id']}.json"))
            validate_record('page_revision',page)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=page['authored_sha256']:
                fail('STALE_INPUT','existing page differs from its reviewed revision')
            key = fingerprint({'kind':subject['kind'],'identity_key':subject['identity_key']})
            backend['existing_pages'][key]={'subject':subject,'page':page,'sha256':page['authored_sha256']}
            view['existing_pages'].append({'identity':{k:subject[k] for k in ('kind','identity_key','canonical_name','aliases')},
                'path':subject['current_path'],'content':path.read_text(encoding='utf-8'),
                'qa_status':page['qa_status'],'business_status':page['business_status'],'visibility':page['visibility']})
    result = {'contract':'fixed-reduce-input/v1','binding':dict(binding),'view':view,'backend':backend}
    result['input_id']=fingerprint(result)
    return result


def prepare(adapter, binding):
    adapter.worker_begin(binding)
    value = snapshot(adapter,binding)
    ref = f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-inputs/{value['input_id']}.json"
    _write_atomic(_vault_path(adapter.workflow.vault,ref),_json_bytes(value))
    return ref,value


def expand(value, draft):
    kind = value['backend']['kind']; validate(kind,draft)
    if 'blocked_reason' in draft: fail('SEMANTIC_REVIEW_REQUIRED',draft['blocked_reason'])
    backend=value['backend']; mapping=backend['mapping']; used=set()
    def refs(handles):
        result=[]
        for handle in handles:
            if handle not in mapping: fail('UNRESOLVED_REFERENCE','candidate handle was not in this observed input')
            if handle in used: fail('DUPLICATE','candidate is used more than once or also omitted')
            used.add(handle); result.extend(copy.deepcopy(mapping[handle]))
        return result
    if kind=='resource-reduce':
        proposals=[{**p,'proposal_id':'proposal-'+str(i),'candidate_refs':refs(p['candidate_refs'])}
            for i,p in enumerate(draft['proposals'],1)]
        request={'batch_id':backend['batch_id'],'actor':backend['actor'],'resource_id':backend['resource_id'],
            'tasks':backend['tasks'],'proposals':proposals,'reason':draft['reason']}
    else:
        decisions=[]
        for page in draft['pages']:
            identity=page['identity']; key=fingerprint({'kind':identity['kind'],'identity_key':identity['identity_key']})
            old=backend['existing_pages'].get(key)
            decisions.append({'candidate_refs':refs(page['candidate_refs']),'identity':identity,
                'action':'update' if old else 'create','path':page['path'],'content':page['content']})
        request={'batch_id':backend['batch_id'],'actor':backend['actor'],
            'resource_reduction_ids':[r['reduction_id'] for r in backend['reductions']],
            'runs':[{'run_id':'run-fixed-'+value['input_id'][:32],'actor':backend['actor'],
                'tasks':backend['tasks'],'expected_registry_revision':backend['identities']['revision'],
                'document_registry_revision':backend['document_registry_revision'],
                'decisions':decisions,'reason':draft['reason']}], 'reason':draft['reason']}
    request['omitted_candidate_refs']=refs(draft['omitted_candidate_refs'])
    if used!=set(mapping): fail('INCOMPLETE_COVERAGE','adopt or explicitly omit every observed citation candidate')
    return request


def submit(adapter, binding, input_ref, draft):
    prefix=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-inputs/"
    if not input_ref.startswith(prefix) or '/' in input_ref[len(prefix):]:
        fail('ACCESS_DENIED','Reduce input must belong to this workflow snapshot directory')
    value=_load_json(_vault_path(adapter.workflow.vault,input_ref))
    if value['binding']!=binding or fingerprint({k:v for k,v in value.items() if k!='input_id'})!=value['input_id']:
        fail('STALE_INPUT','reducer input snapshot changed')
    request=expand(value,draft)
    with worker_binding(binding), workflow_write_guard(adapter.workflow.vault,
            kinds=(value['backend']['kind'],),actor=value['backend']['actor']):
        if snapshot(adapter,binding)['input_id']!=value['input_id']:
            fail('STALE_INPUT','Reduce input changed after observation; no automatic rebind')
        service=adapter.workflow.knowledge
        result=service.reduce_resource(request) if value['backend']['kind']=='resource-reduce' else service.reduce_global(request)
    if not result.get('ok'): fail('REDUCTION_FAILED','Reduce did not persist every requested draft; inspect durable failures')
    receipt={'ok':True,'validated':True,'input_ref':input_ref,
        'reduction_id':result['reduction']['reduction_id'] if 'reduction' in result else result['coordination']['coordination_id']}
    ref=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/worker-receipts/reduce-{fingerprint(receipt)}.json"
    _write_atomic(_vault_path(adapter.workflow.vault,ref),_json_bytes(receipt))
    return {'ok':True,'validated':True,'receipt_ref':ref}
