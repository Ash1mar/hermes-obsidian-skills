"""Measured cost examples for Hermes routing; never semantic page identities."""
import copy
from contextvars import ContextVar
from contextlib import contextmanager
from hermes_source_units.validation import fingerprint
from fixed_semantic_schema import schema,schema_for

CONTRACT='shared-reading-page-budget/v1'
_SCOPE=ContextVar('page_planning_costs',default=None)

@contextmanager
def operation(value):
    token=_SCOPE.set({'value':value,'costs':{}})
    try:yield
    finally:_SCOPE.reset(token)

def probe(value,handles,project):
    """Render a whole provisional page; shared readings are selected once."""
    from input_budget import limit as input_limit
    limit=input_limit(value,'page-pair')
    scope=_SCOPE.get();key=tuple(handles)
    if scope is not None and scope['value'] is value and key in scope['costs']:
        return copy.deepcopy(scope['costs'][key])
    from fixed_reduce import page_view
    from fixed_page_evidence import MARKER,CONTRACT as V1,COMPACT_CONTRACT
    page={'identity':{'kind':'entity','identity_key':'cost-probe-only','canonical_name':'Cost probe only','aliases':[]},
        'path':'entities/cost-probe-only.md','candidate_refs':list(handles)}
    hypothetical={**value,'backend':{**value['backend'],'existing_pages':{}}}
    view=page_view(None,hypothetical,page);view['page_scope_contract']='assigned-candidates/v1'
    from page_protocol import strict
    if strict(value['view']):
        from page_request import prepare
        actual=prepare([(page,view)],project,[],limit=limit,budget=value.get('backend',{}).get('input_budget'))[2]
        selected=actual
    else:
        variants=[view,{**view,MARKER:V1},{**view,MARKER:COMPACT_CONTRACT}]
        projections=[project('page-write',v,schema('page-write'),[]) for v in variants]
        fitting=[p for p in projections if p['input_size']<=limit] or projections
        selected=min(fitting,key=lambda p:p['input_size'])
        slot={'contract':'durable-page-calls/v1','pages':{'p1':{k:v for k,v in view.items() if k not in ('original_evidence',MARKER)}},
            'original_evidence':view.get('original_evidence',{}),'page_members':{'p1':fingerprint(page['identity'])}}
        options=[project('page-pair',v,schema_for('page-pair',slot),[]) for v in (slot,{**slot,MARKER:'lossless-page-pair/v1'})]
        slot_fit=[p for p in options if p['input_size']<=limit] or options
        actual=min(slot_fit,key=lambda p:p['input_size'])
    evidence=view.get('original_evidence',{})
    result={'candidate_refs':list(handles),'reading_refs':[t['task'] for t in evidence.get('tasks',[])],
        'complete_request_codepoints':max(selected['input_size'],actual['input_size']),
        'rendered_utf8_bytes':actual.get('request_cost',{}).get('utf8_bytes'),
        'asset_count':sum(bool(m.get('asset')) for t in evidence.get('tasks',[]) for m in t.get('materials',[]))}
    if scope is not None and scope['value'] is value:scope['costs'][key]=copy.deepcopy(result)
    return result

def hints(value,handles,project):
    wanted=set(handles);ordered=[c['candidate'] for c in value['view']['candidates'] if c['candidate'] in wanted]
    from input_budget import limit as input_limit
    examples=[];members=[];limit=input_limit(value,'page-pair');reserve=1024
    for handle in ordered:
        trial=probe(value,[*members,handle],project)
        if members and (trial['complete_request_codepoints']+reserve>limit or trial['asset_count']):
            examples.append(probe(value,members,project));members=[handle]
        else:members.append(handle)
    if members:examples.append(probe(value,members,project))
    return {'contract':CONTRACT,'limit_codepoints':limit,'provisional_identity_reserve_codepoints':reserve,
        'fit_examples':examples,'candidate_count':len(ordered),
        'rule':'Cost examples only, never mandatory semantic groups. Shared complete readings are counted once per request, not summed per candidate. '
               'Choose the fewest independently meaningful pages that fit; preserve every condition, exception, contrary context, QA and required asset. '
               'Asset transport and final identity/parent costs are checked during writing. An indivisible oversize candidate remains held; never drop evidence to fit. '
               'Avoid over-splitting: compare planning plus all writing/review requests, not planning request count alone.'}
