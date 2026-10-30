"""Lossless page evidence presentation; originals and attempt identities stay owned.

Only untouched page inputs opt in. A consumed original or encoded input retains
its exact original wire and finite journal, including rejected attempts.
"""
import copy,json
from hermes_source_units import ContractError

CONTRACT='lossless-page-evidence/v1'
COMPACT_CONTRACT='lossless-page-evidence/v2'
MARKER='evidence_encoding'
INSTRUCTION=' Evidence encoding lossless-page-evidence/v1: a materials table has columns and ordered rows; each row cell uses its column key. missing lists [row,column] cells whose key is absent, not null. Candidate evidence_qa may name an exact list in candidate_qa_pool. Expand these local representations when reading support refs, source spans, core/context roles, dependencies and QA. All original text, images, limits and qualifications remain required; representation is not evidence selection or semantic approval.'
COMPACT_INSTRUCTION=' Evidence encoding lossless-page-evidence/v2: tables contain columns and ordered rows; missing [row,column] means absent, not null. Expand citation_evidence tables and materials tables. Candidate evidence_qa and source_evidence strings name exact values in candidate_qa_pool and source_evidence_pool. Preserve every field, text, condition, limit, source span and image after expansion. This is lossless representation, never evidence removal or approval.'


def size(value):return len(json.dumps(value,ensure_ascii=False,separators=(',',':')))


def table(rows):
    columns=sorted({k for row in rows for k in row})
    result={'columns':columns,'rows':[[row.get(k) for k in columns] for row in rows]}
    missing=[[i,j] for i,row in enumerate(rows) for j,k in enumerate(columns) if k not in row]
    if missing:result['missing']=missing
    return result


def present(view):
    """Exact structural encoding only; never rank, summarize or remove evidence."""
    result=copy.deepcopy(view)
    for task in result.get('original_evidence',{}).get('tasks',[]):
        rows=task['materials'];packed=table(rows)
        if size(packed)<size(rows):task['materials']=packed
    field='candidates' if 'candidates' in result else 'citation_evidence'
    candidates=result.get(field,[]);before=copy.deepcopy(candidates);pool={};lookup={}
    for candidate in candidates:
        if 'evidence_qa' not in candidate:continue
        qa=candidate['evidence_qa'];key=json.dumps(qa,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if key not in lookup:
            lookup[key]='q'+str(len(pool)+1);pool[lookup[key]]=qa
        candidate['evidence_qa']=lookup[key]
    if size(candidates)+size(pool)<size(before):result['candidate_qa_pool']=pool
    else:result[field]=before
    return result

def present_compact(view):
    result=present(view)
    field='candidates' if 'candidates' in result else 'citation_evidence'
    candidates=result.get(field,[]);before=copy.deepcopy(candidates);pool={};lookup={}
    for candidate in candidates:
        if 'source_evidence' not in candidate:continue
        value=candidate['source_evidence'];key=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if key not in lookup:lookup[key]='s'+str(len(pool)+1);pool[lookup[key]]=value
        candidate['source_evidence']=lookup[key]
    if size(candidates)+size(pool)<size(before):result['source_evidence_pool']=pool
    else:result[field]=before
    packed=table(result.get(field,[]))
    if size(packed)<size(result.get(field,[])):result[field]=packed
    return result

def restore_compact(view):
    """Exact inverse for mechanical post-deployment round-trip verification."""
    result=copy.deepcopy(view)
    def rows(value):
        if isinstance(value,list):return value
        missing={tuple(p) for p in value.get('missing',[])}
        return [{k:r[j] for j,k in enumerate(value['columns']) if (i,j) not in missing} for i,r in enumerate(value['rows'])]
    for task in result.get('original_evidence',{}).get('tasks',[]):task['materials']=rows(task['materials'])
    field='candidates' if 'candidates' in result else 'citation_evidence'
    result[field]=rows(result.get(field,[]))
    for candidate in result[field]:
        for key,pool in (('evidence_qa','candidate_qa_pool'),('source_evidence','source_evidence_pool')):
            if pool in result and isinstance(candidate.get(key),str):candidate[key]=result[pool][candidate[key]]
    result.pop('candidate_qa_pool',None);result.pop('source_evidence_pool',None)
    return result


def select(adapter,binding,phase,view,project):
    """Use ordinary historical journals first; no new allowance for old work."""
    from fixed_global_targets import has_legacy_history
    from semantic_output import path_for
    from fixed_semantic_schema import schema_for
    if view.get(MARKER) in (CONTRACT,COMPACT_CONTRACT):return view
    encoded={**view,MARKER:CONTRACT}
    compact={**view,MARKER:COMPACT_CONTRACT}
    if has_legacy_history(adapter,binding,phase,view):
        if path_for(adapter,binding,phase,encoded).exists() or path_for(adapter,binding,phase,compact).exists():
            raise ContractError('SEMANTIC_OUTPUT_HELD','$','both page evidence encodings have consumed history; no allowance reset')
        return view
    if path_for(adapter,binding,phase,encoded).exists() and path_for(adapter,binding,phase,compact).exists():
        raise ContractError('SEMANTIC_OUTPUT_HELD','$','both lossless encodings have history; allowance cannot reset')
    if path_for(adapter,binding,phase,encoded).exists():return encoded
    if path_for(adapter,binding,phase,compact).exists():return compact
    sc=schema_for(phase,view)
    # Includes the decoding instruction and schema, not just a smaller JSON body.
    chosen=min((view,encoded,compact),key=lambda v:project(phase,v,sc,[])['input_size'])
    if phase=='page-write':chosen={**chosen,'page_scope_contract':'assigned-candidates/v1'}
    return chosen
