"""Versioned, capability-free evidence presentation for future native work."""
import copy,os
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_write_atomic,_json_bytes,_exclusive_lock
from hermes_source_units.validation import fingerprint

CONTRACT='efficiency-presentation/v1'

def enabled(adapter,binding):
    checked_binding=binding
    if binding.get('node','').startswith('pass-slice:'):
        workspace=os.environ.get('HERMES_KANBAN_WORKSPACE')
        if workspace:
            checked_binding=_load_json(Path(workspace)/'worker-request.json')
            keys=('workflow_id','node','task_id','actor','template_hash','worker_id')
            if any(k in binding and checked_binding.get(k)!=binding[k] for k in keys):
                raise ContractError('STALE_INPUT','$','presentation worker identity changed')
    adapter.worker_check(checked_binding)
    w=adapter.workflow.status(binding['workflow_id'])
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}"
    path=root/'presentation-contract.json'
    identity={'workflow_id':w['workflow_id'],'scope_sha256':fingerprint(w['scope'])}
    with _exclusive_lock(path.with_suffix('.lock'),timeout=5):
        if path.exists():record=_load_json(path)
        else:
            consumed=any((root/'semantic-calls').glob('*.json')) or any((root/'output-attempts').glob('*.json'))
            budget=root/'pass-call-budget.json'
            if budget.exists():consumed=consumed or any(r.get('attempts') for r in _load_json(budget).get('tasks',{}).values())
            record={**identity,'contract':'legacy' if consumed else CONTRACT}
            _write_atomic(path,_json_bytes(record))
    if any(record.get(k)!=v for k,v in identity.items()) or record.get('contract') not in ('legacy',CONTRACT):
        raise ContractError('STALE_INPUT','$','presentation identity changed')
    return record['contract']==CONTRACT

def qa_present(view):
    """Share identical QA only; keep every other field byte-for-byte in value terms."""
    result=copy.deepcopy(view);pool={};lookup={}
    for c in result.get('candidates',[]):
        if 'evidence_qa' not in c:continue
        key=fingerprint(c['evidence_qa'])
        if key not in lookup:
            lookup[key]='q'+str(len(pool)+1);pool[lookup[key]]=c['evidence_qa']
        c['evidence_qa']=lookup[key]
    if pool:result['candidate_qa_pool']=pool
    return result

def qa_restore(view):
    result=copy.deepcopy(view);pool=result.pop('candidate_qa_pool',{})
    for c in result.get('candidates',[]):
        if isinstance(c.get('evidence_qa'),str) and c['evidence_qa'] in pool:c['evidence_qa']=copy.deepcopy(pool[c['evidence_qa']])
    return result

def page_evidence_view(packet,candidates):
    """Conservative derived view. Unknown relevance always retains a whole package.

    No semantic guesses: dropping a material requires a producer-supplied exact
    dependency closure with complete_context=True. Existing packets have no such
    proof and retain every material, heading, QA and omission notice.
    """
    result=copy.deepcopy(packet);selected=[]
    refs={}
    for c in candidates:
        for link in c.get('source_evidence',[]):
            for ref in link['support_refs']:
                refs.setdefault(link['reading'],set()).add(ref if isinstance(ref,str) else ref['ref'])
    for task in result.get('tasks',[]):
        wanted=refs.get(task['task'],set())
        known={m['ref'] for m in task['materials']}
        if not wanted<=known:raise ContractError('UNRESOLVED_REFERENCE','$','page support not in its exact reading')
        # Until a trusted dependency closure exists, full-package fallback is required.
        selected.append({'reading':task['task'],'support_refs':sorted(wanted),
                         'retained_refs':[m['ref'] for m in task['materials']],
                         'reason':'full_package_context_not_mechanically_separable'})
    return result,{'contract':'page-evidence-view/v1','original_sha256':fingerprint(packet),
                   'candidates_sha256':fingerprint(candidates),'selection':selected,'view_sha256':fingerprint(result)}

def classify_block(draft):
    """Free-text refusals cannot authorize changed identities or extra model calls."""
    if isinstance(draft,dict) and 'blocked_reason' in draft:
        return {'type':'evidence_block','automatic_correction':False,
                'reason':'unchanged evidence; preserve refusal and isolate dependencies'}
    return {'type':'mechanical_output','automatic_correction':True}


def pair_preview(pages,measure,*,limit=30000):
    """Explicit offline two-slot experiment; no dispatcher, journal or model access.

    Independent native page execution remains the default. This cannot grant a
    batch call until an independently versioned per-page durable budget exists.
    """
    if len(pages)!=2 or any(p.get('risk_reasons') or p.get('qa_unresolved') or not p.get('evidence_complete') for p in pages):
        raise ContractError('ACCESS_DENIED','$','pair requires two independently complete low-risk pages')
    if pages[0].get('source_scope')!=pages[1].get('source_scope'):
        raise ContractError('ACCESS_DENIED','$','pair scopes differ')
    if any(not p.get('source_scope') for p in pages):raise ContractError('ACCESS_DENIED','$','scope proof required')
    view={'contract':'two-page-preview/v1','pages':{'p1':copy.deepcopy(pages[0]),'p2':copy.deepcopy(pages[1])}}
    size=measure(view)
    if size>limit:raise ContractError('READING_WINDOW_OVERSIZE','$','pair exceeds complete request budget')
    return {**view,'input_size':size,'dispatch_authorized':False,'additional_checks_per_page':1}
