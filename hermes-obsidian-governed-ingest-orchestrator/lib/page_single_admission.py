"""Ephemeral reuse of completed single-dispatch checks; never a call allowance."""
from hermes_source_units.validation import fingerprint
from page_pair_calls import key
CONTRACT='single-admission-body-pairs/v1'

def enabled(view):return view.get('body_pairing_contract')==CONTRACT

def admit(page,view,size,limit,*,allowed,qualification=None,budget=None,basis='current single-dispatch checks'):
    from page_pair_eligibility import inspect
    q=qualification or inspect(view)
    images=any(m.get('asset') for t in view.get('original_evidence',{}).get('tasks',[]) for m in t.get('materials',[]))
    evidence=view.get('original_evidence',{})
    from fixed_semantic import system_prompt
    system={'body_call_contract':'independent-page-body/v1','page_execution_contract':view.get('page_execution_contract')}
    if view.get('page_semantics_policy'):system['page_semantics_policy']=view['page_semantics_policy']
    return {'contract':CONTRACT,'page_key':key(page),'view_sha256':fingerprint(view),
        'allowed':bool(allowed and size<=limit and not q['required_missing']),
        'single_input_codepoints':size,'limit':limit,'needs_image_transport':images,
        'budget_sha256':budget.get('budget_sha256') if budget else None,'basis':basis,
        'qualification':q,
        'source_bundle_sha256':fingerprint({'original_evidence':evidence,'source_proof':view.get('evidence_selection',{}).get('readings')}),
        'original_text_codepoints':sum(len(v) for v in evidence.get('texts',{}).values() if isinstance(v,str)),
        'system_codepoints_floor':len(system_prompt('page-pair',system)),
        'publication_eligibility_granted':False,'semantic_ordinals_granted':0}

def matches(item,proof):
    page,view=item
    return bool(proof and proof.get('contract')==CONTRACT and proof.get('allowed') is True
        and proof.get('page_key')==key(page) and proof.get('view_sha256')==fingerprint(view)
        and proof['single_input_codepoints']<=proof['limit'])

def pair_input_lower_bound(left,right):
    """Only prune impossible pairs. Every accepted pair still renders in full.

    The current lossless body codec shares only exact whole evidence bundles.
    Required literal original text plus the shorter raw system is a lower bound
    for both raw and encoded complete requests, excluding all other overhead.
    """
    a,b=left['original_text_codepoints'],right['original_text_codepoints']
    texts=max(a,b) if left['source_bundle_sha256']==right['source_bundle_sha256'] else a+b
    return texts+min(left['system_codepoints_floor'],right['system_codepoints_floor'])
