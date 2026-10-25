"""Exact alpha-renaming proofs for native page inputs, without semantic rewriting."""
import copy
from hermes_source_units.citation_amendment import fail
from hermes_source_units.validation import fingerprint
from fixed_semantic_recovery import task_content

def candidate(view,c):
    packet=view['original_evidence'];tasks={t['task']:task_content(packet,t) for t in packet['tasks']}
    result=copy.deepcopy(c);result.pop('candidate',None)
    result['source_evidence']=sorted([
        {'original_task_sha256':tasks[r['reading']],'support_refs':r['support_refs']}
        for r in c['source_evidence']],key=fingerprint)
    return result

def page_input(view):
    if set(view)-{'identity','path','candidates','original_evidence','existing_page','coverage_limitations'}:
        fail('unrecognized writer input cannot be normalized for reuse')
    packet=view['original_evidence']
    return {**{k:copy.deepcopy(v) for k,v in view.items() if k not in ('candidates','original_evidence')},
            'candidates':sorted([candidate(view,c) for c in view['candidates']],key=fingerprint),
            'complete_original_tasks':sorted(task_content(packet,t) for t in packet['tasks']),
            'original_qa_rule':packet.get('qa_rule')}

def aliases(old,new,old_pass,new_pass):
    def convert(ref):return {**ref,'pass_id':new_pass if ref['pass_id']==old_pass else ref['pass_id']}
    wanted={fingerprint(sorted(refs,key=fingerprint)):h for h,refs in new['backend']['mapping'].items()}
    mapping={}
    for handle,refs in old['backend']['mapping'].items():
        digest=fingerprint(sorted([convert(r) for r in refs],key=fingerprint))
        if digest not in wanted:fail('candidate deduplication/membership changed; cannot prove a bounded alias mapping')
        mapping[handle]=wanted[digest]
    if len(set(mapping.values()))!=len(mapping) or set(mapping.values())!=set(wanted.values()):
        fail('candidate mapping is not a bijection')
    return mapping

def rebase_page(page,mapping):
    return {k:(list(dict.fromkeys(mapping[h] for h in v)) if k=='candidate_refs' else copy.deepcopy(v))
            for k,v in page.items() if k in ('identity','path','candidate_refs')}

def page_proof(old_value,new_value,old_page,new_page,adapter):
    import fixed_reduce
    old=fixed_reduce.page_view(adapter,old_value,old_page)
    new=fixed_reduce.page_view(adapter,new_value,new_page)
    left=page_input(old);right=page_input(new)
    if left!=right:fail('current full page input differs; retain affected work, do not automatically regenerate')
    return {'contract':'citation-local-page-reuse/v1','old_input_sha256':fingerprint(old),
            'new_input_sha256':fingerprint(new),'normalized_input_sha256':fingerprint(left),
            'identity_path_parent_conditions_qa_and_complete_materials_equal':True}
