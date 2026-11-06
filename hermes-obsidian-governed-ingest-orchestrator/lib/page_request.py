"""One complete request constructor for future planning, singles and pairs."""
import copy
from page_protocol import CONTRACT,strict
from page_pair_calls import key,CONTRACT as WIRE
from fixed_semantic_schema import schema_for
from request_cost import rank

def prepare(group,project,images=(),diagnostics=None,*,limit=30000):
    view={'contract':WIRE,'page_execution_contract':CONTRACT,
        'pages':{f'p{i}':{k:copy.deepcopy(v) for k,v in page.items() if k not in ('original_evidence','evidence_encoding')} for i,(_,page) in enumerate(group,1)},
        'original_evidence':copy.deepcopy(group[0][1].get('original_evidence',{})),
        'page_members':{f'p{i}':key(p) for i,(p,_) in enumerate(group,1)}}
    if diagnostics:view['page_validation_diagnostics']=copy.deepcopy(diagnostics)
    schema=schema_for('page-pair',view)
    variants=[(v,project('page-pair',v,schema,images)) for v in (view,{**view,'evidence_encoding':'lossless-page-pair/v1'})]
    fitting=[v for v in variants if v[1]['input_size']<=limit] or variants
    selected=min(fitting,key=lambda v:rank(v[1]))
    return selected[0],schema,selected[1],variants
