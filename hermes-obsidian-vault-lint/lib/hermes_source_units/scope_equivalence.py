"""Sufficient identity proof, never a semantic similarity or relevance filter."""
import re
from .validation import fingerprint

POLICY='explicit-page-risks/v2'
def literal(value):
    if isinstance(value,str):return re.sub(r'\s+',' ',value).strip()
    if isinstance(value,list):return [literal(v) for v in value]
    if isinstance(value,dict):return {k:literal(v) for k,v in value.items()}
    return value

def prove(selected):
    """Only remove a lexical scope trigger with identical original provenance.

    Source spans and all inspections stay byte-exact. Only whitespace runs in
    explicit claim/scope fields normalize; numbers, operators and order stay.
    """
    rows=[]
    for record,c in selected:
        if not c.get('support_refs') or not record.get('inspections'):
            return {'equivalent':False,'reason':'missing_original_provenance'}
        rows.append({'source':c['support_refs'],'inspections':record['inspections'],
            'claim':{k:literal(c.get(k)) for k in ('finding','applicability','conditions','exceptions')}})
    same=bool(rows) and all(r==rows[0] for r in rows)
    return {'equivalent':same,'reason':'identical_source_original_scope_conditions_exceptions' if same else 'equivalence_not_proven',
        'proof_sha256':fingerprint(rows),'members':len(rows)}
