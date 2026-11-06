"""Ephemeral reuse of completed single-dispatch checks; never a call allowance."""
from hermes_source_units.validation import fingerprint
from page_pair_calls import key
CONTRACT='single-admission-body-pairs/v1'

def enabled(view):return view.get('body_pairing_contract')==CONTRACT

def admit(page,view,size,limit,*,allowed,qualification=None,budget=None,basis='current single-dispatch checks'):
    from page_pair_eligibility import inspect
    q=qualification or inspect(view)
    images=any(m.get('asset') for t in view.get('original_evidence',{}).get('tasks',[]) for m in t.get('materials',[]))
    return {'contract':CONTRACT,'page_key':key(page),'view_sha256':fingerprint(view),
        'allowed':bool(allowed and size<=limit and not q['required_missing']),
        'single_input_codepoints':size,'limit':limit,'needs_image_transport':images,
        'budget_sha256':budget.get('budget_sha256') if budget else None,'basis':basis,
        'qualification':q,
        'publication_eligibility_granted':False,'semantic_ordinals_granted':0}

def matches(item,proof):
    page,view=item
    return bool(proof and proof.get('contract')==CONTRACT and proof.get('allowed') is True
        and proof.get('page_key')==key(page) and proof.get('view_sha256')==fingerprint(view)
        and proof['single_input_codepoints']<=proof['limit'])
