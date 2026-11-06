"""Different evidence per body slot; lossless exact-bundle sharing only."""
import copy,json
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
CONTRACT='independent-page-body/v1'
ENCODING='independent-body-evidence/v1'

def enabled(view):return view.get('body_call_contract')==CONTRACT

def present(view):
    from fixed_page_evidence import present_compact
    v=copy.deepcopy(view);pool={}
    for slot,p in v['pages'].items():
        p=present_compact(p);e=p.pop('original_evidence')
        # Scope/QA/required assets and source mappings are part of the identity.
        identity={'original_evidence':e,'source_proof':p.get('evidence_selection',{}).get('readings')}
        k=fingerprint(identity);pool[k]=identity;p['original_evidence_ref']=k;v['pages'][slot]=p
    v['evidence_pool']=pool
    return v

def restore(view):
    from hermes_source_units.review_receipts import restore_page
    v=copy.deepcopy(view);pool=v.pop('evidence_pool',{})
    for slot,p in v['pages'].items():
        if 'original_evidence_ref' in p:
            ref=p.pop('original_evidence_ref');entry=pool[ref]
            if fingerprint(entry)!=ref:raise ContractError('SOURCE_CHANGED','$','shared evidence pool changed')
            p['original_evidence']=copy.deepcopy(entry['original_evidence'])
        v['pages'][slot]=restore_page(p)
    v.pop('evidence_encoding',None)
    return v

def prepare(group,project,images=(),diagnostics=None,*,limit=40000,budget=None):
    from page_pair_calls import key,CONTRACT as WIRE
    from page_protocol import CONTRACT as PROTOCOL
    from fixed_semantic_schema import schema_for
    from input_budget import check_request
    from request_cost import rank
    if not 1<=len(group)<=2:raise ContractError('INVALID_SCHEMA','$','independent bodies currently support at most two slots')
    v={'contract':WIRE,'body_call_contract':CONTRACT,'page_execution_contract':PROTOCOL,
       'pages':{f'p{i}':copy.deepcopy(p) for i,(_,p) in enumerate(group,1)},
       'page_members':{f'p{i}':key(p) for i,(p,_) in enumerate(group,1)}}
    if diagnostics:v['page_validation_diagnostics']=copy.deepcopy(diagnostics)
    policies={p.get('page_semantics_policy') for _,p in group}
    if len(policies)==1 and None not in policies:v['page_semantics_policy']=next(iter(policies))
    pairing={p.get('body_pairing_contract') for _,p in group}
    if len(pairing)==1 and None not in pairing:v['body_pairing_contract']=next(iter(pairing))
    if images:
        # Asset qualification keeps image pages single; never infer ownership
        # from coincident local task/material IDs across independent pages.
        if len(group)!=1:raise ContractError('PAGE_PROTOCOL_UNSUPPORTED','$','image bodies require an independent single slot')
        v['image_bindings']={'p1':list(range(len(images)))}
    else:v['image_bindings']={s:[] for s in v['pages']}
    sc=schema_for('page-pair',v);variants=[]
    estimate=sum(max(512,len(json.dumps(p.get('candidates',[]),ensure_ascii=False).encode())//3+1024) for _,p in group)
    for row in (v,{**v,'evidence_encoding':ENCODING}):
        p=project('page-pair',row,sc,images)
        usable=7168
        if budget:p['budget_check']=check_request(p,budget,'page-pair');usable=p['budget_check']['usable_output_tokens']
        else:p['budget_check']={'fits':p['input_size']<=limit,'split_reasons':[] if p['input_size']<=limit else ['input_codepoints'],'usable_output_tokens':usable}
        # A single page has no pair-output penalty. Actual truncation still counts.
        if len(group)>1 and estimate>usable:p['budget_check']['fits']=False;p['budget_check']['split_reasons'].append('estimated_body_output_headroom')
        p['estimated_body_output_tokens']=estimate;variants.append((row,p))
    chosen=min([x for x in variants if x[1]['budget_check']['fits']] or variants,key=lambda x:rank(x[1]))
    return chosen[0],sc,chosen[1],variants

def images_for(adapter,view):
    from review_pair_request import images_for as collect
    return collect(adapter,view)
