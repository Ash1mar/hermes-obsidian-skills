"""Reproducible complete-slot oversize proof; no model output or allowance."""
from hermes_source_units.validation import fingerprint
from page_pair_calls import CONTRACT,key

def proof(adapter,value,page):
    import fixed_reduce
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_schema import schema_for
    from fixed_semantic_media import image_catalog,select_images,require_images_fit
    from request_cost import rank
    view=fixed_reduce.page_view(adapter,value,page);view['page_scope_contract']='assigned-candidates/v1'
    evidence=view['original_evidence'];images=[]
    if any(m.get('asset') for t in evidence.get('tasks',[]) for m in t.get('materials',[])):
        images=select_images(evidence['tasks'],image_catalog(adapter,evidence));require_images_fit(images)
    wire={'contract':CONTRACT,'pages':{'p1':{k:v for k,v in view.items() if k!='original_evidence'}},
        'original_evidence':evidence,'page_members':{'p1':key(page)}}
    sc=schema_for('page-pair',wire)
    variants=[prepare_request_projection('page-pair',v,sc,images) for v in (wire,{**wire,'evidence_encoding':'lossless-page-pair/v1'})]
    from page_protocol import strict
    if strict(value['view']):
        from page_request import prepare
        _,sc,_,choices=prepare([(page,view)],prepare_request_projection,images)
        variants=[p for _,p in choices]
    # Isolation requires EVERY supported full representation to exceed the bound.
    size=min(p['input_size'] for p in variants)
    return {'contract':'complete-slot-budget/v1','page_key':key(page),'input_sha256':fingerprint(view),
        'minimum_complete_input_codepoints':size,'limit':30000,
        'wire_sha256s':[fingerprint(p['wire']) for p in variants],
        'schema_sha256':fingerprint(sc),'new_calls_granted':0}

def verified(adapter,value,page,held):
    if (held.get('code')!='READING_WINDOW_OVERSIZE' or held.get('origin')!='complete-slot-budget/v1'
            or not isinstance(held.get('budget_proof'),dict)):return False
    actual=proof(adapter,value,page)
    return actual==held['budget_proof'] and actual['minimum_complete_input_codepoints']>actual['limit']
