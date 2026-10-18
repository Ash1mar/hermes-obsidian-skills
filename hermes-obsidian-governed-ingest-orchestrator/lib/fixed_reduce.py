"""Observed Reduce inputs and mechanical expansion of short candidate handles."""
import copy
import hashlib

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint, validate_record
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard
from fixed_semantic_schema import validate
import fixed_evidence


def fail(code, message):
    raise ContractError(code, '$', message)


def snapshot(adapter, binding):
    checked = adapter.worker_check(binding)
    return _snapshot(adapter,binding,checked)


def preview_snapshot(adapter,binding):
    """Read-only operator planning; this never grants worker write authority."""
    import os
    workflow=adapter.workflow.status(binding['workflow_id'])
    if (os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')
            or workflow['state']!='cancelled' or not workflow['cancel_requested']):
        fail('ACCESS_DENIED','input preview requires a stopped trusted operator')
    if not any(c['task_id']==binding['task_id'] and c['node']==binding['node']
               for c in workflow['kanban']['task_map']):
        fail('STALE_INPUT','preview must name the current native binding')
    return _snapshot(adapter,binding,{'batch_id':workflow['batch_id']})


def _snapshot(adapter,binding,checked):
    workflow = adapter.workflow.status(binding['workflow_id'])
    service = adapter.workflow.knowledge
    batch = service._batch(checked['batch_id'])
    kind, _, resource = binding['node'].partition(':')
    adapter.workflow.verify_pass_skips(workflow)
    tasks = [service._task(tid) for tid in batch['task_ids'] if tid not in adapter.workflow.skipped_pass_tasks(workflow)]
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
    if workflow.get('pass_skip_history'):
        view['coverage_limitations']={'excluded_task_count':len(adapter.workflow.skipped_pass_tasks(workflow)),
            'note':'Some planned source ranges were explicitly skipped. They provide no evidence and must not be inferred or described as fully covered.'}
    packet, readings = fixed_evidence.original_evidence(service,
        [pair[0] for pair in eligible.values()], workflow['actor'], service._document_registry_revision())
    view['original_evidence'] = packet
    mapping, aliases, grouped = {}, {}, {}
    # Deduplicate only identical semantics AND inspection QA, never similar names.
    for key, (record,candidate) in sorted(eligible.items()):
        observation = {k:copy.deepcopy(v) for k,v in candidate.items() if k not in ('candidate_id','support_refs')}
        observation['evidence_qa'] = [{k:v for k,v in i.items() if k!='source_ref'} for i in record['inspections']]
        # A source packet's indivisible unit is one citation plus its complete
        # original package. Do not fuse identical claims across distinct readings
        # into an unbounded evidence bundle before budget partitioning.
        digest = fingerprint({'observation':observation,'reading':readings[(record['task_id'],record['reading_package_id'])]['reading']}
                             if kind=='resource-reduce' else observation)
        handle = grouped.get(digest)
        if handle is None:
            handle = 'c'+str(len(mapping)+1); grouped[digest]=handle; mapping[handle]=[]
            view['candidates'].append({'candidate':handle,**observation,'source_evidence':[]})
        exposed = next(c for c in view['candidates'] if c['candidate']==handle)
        link = fixed_evidence.support(service,record,candidate,readings)
        if link not in exposed['source_evidence']: exposed['source_evidence'].append(link)
        mapping[handle].append({'pass_id':key[0],'candidate_id':key[1]})
        aliases[key]=handle
    backend = {'kind':kind,'batch_id':batch['batch_id'],'actor':workflow['actor'],
        'resource_id':resource,'tasks':[{'task_id':t['task_id'],'revision':t['revision']} for t in tasks],
        'task_hashes':[fingerprint(t) for t in tasks], 'mapping':mapping,
        'passes':[fingerprint(p) for tid in sorted(passes) for p in passes[tid]],
        'document_registry_revision':service._document_registry_revision(),
        'reductions':reductions,'identities':None,'existing_pages':{}}
    backend['coverage_gap_reports']=workflow.get('pass_skip_history',[])
    if kind=='global-reduce':
        view['proposals'] = [{'proposal':'p'+str(index), 'summary':p['summary'],
            'source_resource_id':reduction['resource_id'],
            'source_path':next((o['path'] for o in workflow['source_outcomes'] if o.get('resource_id')==reduction['resource_id']),None),
            'candidate_refs':list(dict.fromkeys(aliases[(r['pass_id'],r['candidate_id'])] for r in p['candidate_refs'])),
            'identity_hints':p['identity_hints'],'path_hints':p['path_hints']}
            for index,(reduction,p) in enumerate(((reduction,p) for reduction in reductions for p in reduction['proposals']),1)]
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
                'path':subject['current_path'],
                'qa_status':page['qa_status'],'business_status':page['business_status'],'visibility':page['visibility']})
    result = {'contract':'fixed-reduce-input/v1','binding':dict(binding),'view':view,'backend':backend}
    if kind=='resource-reduce':
        from source_hold_recovery import rebind_view
        result=rebind_view(adapter,workflow,binding,result)
    result['input_id']=fingerprint(result)
    return result


def prepare(adapter, binding):
    adapter.worker_begin(binding)
    value = snapshot(adapter,binding)
    ref = f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-inputs/{value['input_id']}.json"
    _write_atomic(_vault_path(adapter.workflow.vault,ref),_json_bytes(value))
    return ref,value


def global_plan_view(value):
    """Provisional routing only; full evidence remains in the bound snapshot."""
    original=value['view']
    view={k:copy.deepcopy(original[k]) for k in ('phase','existing_pages','coverage_limitations') if k in original}
    columns={'proposals':['proposal','summary','candidate_refs','identity_hints','path_hints'],
             'candidates':['candidate','kind','name','applicability','conditions','exceptions','evidence_qa']}
    if any('source_resource_id' in p for p in original.get('proposals',[])):
        columns['proposals']+=['source_resource_id','source_path']
    view.update(columns=columns,proposals=[[copy.deepcopy(p.get(k)) for k in columns['proposals']]
        for p in original.get('proposals',[])],candidates=[],qa={})
    qa_keys={}
    for candidate in original['candidates']:
        row={k:copy.deepcopy(candidate[k]) for k in ('candidate','kind','name','applicability','conditions','exceptions') if k in candidate}
        qa=[]
        for item in candidate.get('evidence_qa',[]):
            boundary={k:v for k,v in item.items() if k!='finding' or item.get('qa')!='usable'}
            if boundary not in qa:qa.append(copy.deepcopy(boundary))
        refs=[]
        for boundary in qa:
            digest=fingerprint(boundary)
            if digest not in qa_keys:
                qa_keys[digest]='q'+str(len(qa_keys)+1)
                view['qa'][qa_keys[digest]]=boundary
            refs.append(qa_keys[digest])
        row['evidence_qa']=refs
        view['candidates'].append([row.get(k) for k in columns['candidates']])
    return view


def page_view(adapter, value, page):
    """Exact selected prior content, never a model-controlled file lookup."""
    backend=value['backend']; identity=page['identity']
    key=fingerprint({'kind':identity['kind'],'identity_key':identity['identity_key']})
    old=backend['existing_pages'].get(key); parent=None
    if old:
        path=_vault_path(adapter.workflow.vault,old['subject']['current_path'])
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=old['sha256']: fail('STALE_INPUT','selected parent changed after planning')
        parent={'identity':{k:old['subject'][k] for k in ('kind','identity_key','canonical_name','aliases')},
            'path':old['subject']['current_path'],'content':raw.decode('utf-8'),
            'qa_status':old['page']['qa_status'],'business_status':old['page']['business_status'],
            'visibility':old['page']['visibility']}
    handles=set(page['candidate_refs'])
    candidates=[c for c in value['view']['candidates'] if c['candidate'] in handles]
    view={'identity':identity,'path':page['path'],'candidates':candidates,'existing_page':parent}
    if 'coverage_limitations' in value['view']:
        view['coverage_limitations']=copy.deepcopy(value['view']['coverage_limitations'])
    if 'original_evidence' in value['view']:
        readings={r['reading'] for c in candidates for r in c['source_evidence']}
        view['original_evidence']=fixed_evidence.selected(value['view']['original_evidence'],readings)
    return view


def resource_view(value, handles):
    candidates=[c for c in value['view']['candidates'] if c['candidate'] in handles]
    result={k:copy.deepcopy(v) for k,v in value['view'].items() if k not in ('candidates','original_evidence')}
    result['candidates']=copy.deepcopy(candidates)
    readings={r['reading'] for c in candidates for r in c['source_evidence']}
    result['original_evidence']=fixed_evidence.selected(value['view']['original_evidence'],readings)
    return result


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
