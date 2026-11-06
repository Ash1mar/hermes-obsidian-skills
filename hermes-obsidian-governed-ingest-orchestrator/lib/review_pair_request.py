"""One complete request with one/two page-local review scopes and evidence sets."""
import copy
from hermes_source_units import ContractError
from hermes_source_units.review_receipts import CONTRACT,ENCODING,restore_page
from hermes_source_units.validation import fingerprint
from page_pair_calls import key

def canonical(view,page_id,assessment,revision_id=None):
    v=restore_page({k:copy.deepcopy(x) for k,x in view.items() if k!='page'})
    v['review_page_id']=page_id
    if revision_id:v['review_revision_id']=revision_id
    from fixed_page_evidence import review_scope
    v['review_scope']=review_scope(assessment)
    if 'evidence_selection' not in v:
        from efficiency_presentation import page_evidence_view
        v['original_evidence'],v['evidence_selection']=page_evidence_view(v['original_evidence'],v['citation_evidence'])
    return v

def present(view):
    from fixed_page_evidence import present_compact
    result=copy.deepcopy(view)
    for slot,page in result['pages'].items():result['pages'][slot]=present_compact(page)
    return result

def images_for(adapter,view):
    from fixed_semantic_media import image_catalog,select_images,require_images_fit
    images=[];bindings={};seen={}
    for slot,page in view['pages'].items():
        e=page['original_evidence'];local=select_images(e['tasks'],image_catalog(adapter,e));bindings[slot]=[]
        for image in local:
            k=fingerprint({k:v for k,v in image.items() if k!='url'})
            if k not in seen:seen[k]=len(images);images.append(image)
            bindings[slot].append(seen[k])
    require_images_fit(images)
    if 'image_bindings' in view and view['image_bindings']!=bindings:
        raise ContractError('SOURCE_CHANGED','$','page-local image ownership changed')
    return images,bindings

def prepare(group,project,*,adapter=None,budget=None,images=None):
    if not 1<=len(group)<=2:raise ContractError('INVALID_SCHEMA','$','only one/two independent reviews supported')
    from fixed_semantic_schema import schema_for
    from input_budget import profile,check_request
    from request_cost import rank
    view={'contract':CONTRACT,'pages':{f'p{i}':copy.deepcopy(v) for i,(p,v) in enumerate(group,1)},
        'page_members':{f'p{i}':key(p) for i,(p,v) in enumerate(group,1)}}
    if images is None:
        if adapter is None:images=[];bindings={slot:[] for slot in view['pages']}
        else:images,bindings=images_for(adapter,view)
    else:bindings={slot:[] for slot in view['pages']}
    view['image_bindings']=bindings;sc=schema_for('page-review-pair',view)
    choices=[(v,project('page-review-pair',v,sc,images)) for v in (view,{**view,'evidence_encoding':ENCODING})]
    budget=budget or profile('review-40000')
    for _,p in choices:
        p['budget_check']=check_request(p,budget,'page-review-pair')
        p['estimated_review_output_tokens']=1024*len(group)
        if p['estimated_review_output_tokens']>p['budget_check']['usable_output_tokens']:
            p['budget_check']['fits']=False;p['budget_check']['split_reasons'].append('estimated_review_output_headroom')
    fitting=[v for v in choices if v[1]['budget_check']['fits']]
    selected=min(fitting or choices,key=lambda v:rank(v[1]))
    return selected[0],sc,selected[1],images,choices
