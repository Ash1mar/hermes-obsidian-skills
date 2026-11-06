"""Vault-only verification of one independently persisted native review slot."""
import copy,hashlib,json
from contextvars import ContextVar
from contextlib import contextmanager
from .source_units import _load_json,_vault_path
from .validation import ContractError,fingerprint

CONTRACT='independent-page-review/v1'
ENCODING='independent-page-evidence/v1'
_READS=ContextVar('review_receipt_verification',default=None)
@contextmanager
def verification_scope():
    if _READS.get() is not None:yield;return
    token=_READS.set({})
    try:yield
    finally:_READS.reset(token)
def fail(message):raise ContractError('REVIEW_REQUIRED','$.reviews',message)

def restore_page(view):
    result=copy.deepcopy(view)
    def rows(value):
        if isinstance(value,list):return value
        missing={tuple(p) for p in value.get('missing',[])}
        return [{k:r[j] for j,k in enumerate(value['columns']) if (i,j) not in missing} for i,r in enumerate(value['rows'])]
    for t in result.get('original_evidence',{}).get('tasks',[]):t['materials']=rows(t['materials'])
    field='candidates' if 'candidates' in result else 'citation_evidence'
    result[field]=rows(result.get(field,[]))
    for c in result[field]:
        for k,pool in (('evidence_qa','candidate_qa_pool'),('source_evidence','source_evidence_pool')):
            if pool in result and isinstance(c.get(k),str):c[k]=copy.deepcopy(result[pool][c[k]])
    result.pop('candidate_qa_pool',None);result.pop('source_evidence_pool',None);result.pop('evidence_encoding',None)
    return result

def decode(raw):
    text=raw.strip()
    if text.startswith('<final>') and text.endswith('</final>'):text=text[7:-8].strip()
    elif text.endswith('</final>'):text=text[:-8].strip()
    def unique(pairs):
        value={}
        for k,v in pairs:
            if k in value:raise ValueError('duplicate review response key')
            value[k]=v
        return value
    return json.loads(text,object_pairs_hook=unique)

def payload(vault,root,pointer):
    p=_vault_path(vault,pointer['ref'])
    if not p.is_relative_to(root):fail('foreign review payload')
    scope=_READS.get();key=(str(p),pointer['sha256'])
    if scope is not None and key in scope:return scope[key]
    from .read_accounting import _raw_read_bytes
    raw=_raw_read_bytes(p,'review_receipt.payload')
    if hashlib.sha256(raw).hexdigest()!=pointer['sha256']:fail('review payload fingerprint differs')
    value=json.loads(raw)
    if scope is not None:scope[key]=value
    return value

def audit(vault,ref):
    scope=_READS.get();key=('audit',str(vault),ref)
    if scope is not None and key in scope:return scope[key]
    value=_load_json(_vault_path(vault,ref))
    if scope is not None:scope[key]=value
    return value

def validate_receipt(service,page,review,acceptance,audit,expected):
    vault=service.vault;wid=audit['workflow_id'];root=vault/f'_system/ledgers/ingest-workflows/{wid}/review-calls'
    pointer=acceptance.get('review_receipt')
    if not pointer:fail('paired approval requires its own durable receipt')
    r=payload(vault,root,pointer);key=fingerprint({'kind':'review','identity_key':page['page_id']})
    if (r.get('state')!='valid' or r.get('page_key')!=key or r.get('ordinal') not in (1,2)
            or r.get('receipt_id')!=fingerprint({k:v for k,v in r.items() if k!='receipt_id'})
            or r.get('audit_ref')!=acceptance['review_audit_ref']
            or r.get('result')!={'decision':'approved','review_note':review['note']}
            or r.get('saved',{}).get('page_id')!=page['page_id']
            or r['saved'].get('authored_sha256')!=page['authored_sha256']
            or r['saved'].get('assessment')!=expected):fail('independent page review receipt differs')
    scope=_READS.get();index_key=(str(root),'index')
    if scope is not None and index_key in scope:index=scope[index_key]
    else:
        index=_load_json(root/'page-call-budget.json')
        if scope is not None:scope[index_key]=index
    entry=index['requests'][r['request_id']]
    q=payload(vault,root,{'ref':(root/entry['payload']['ref']).relative_to(vault).as_posix(),'sha256':entry['payload']['sha256']})
    members=[[m['page_key'],m['ordinal']] for m in q['members'].values()]
    identity={'contract':'durable-page-calls/v1','members':members,'wire':q['wire'],'schema':q['schema'],'epoch':q['epoch']}
    if q.get('image_inputs'):identity['image_inputs']=q['image_inputs']
    if fingerprint(identity)!=r['request_id'] or len(members) not in (1,2):fail('review request identity changed')
    slots=[s for s,m in q['members'].items() if m=={'page_key':key,'ordinal':r['ordinal']}]
    if len(slots)!=1:fail('review result has no unique page slot')
    slot=slots[0];wire=q['wire'];header,sep,body=wire['user'].partition('\n')
    if header!='Semantic input v1' or not sep:fail('unsupported review wire')
    data=json.loads(body);view=data['input'];canonical=restore_page(view['pages'][slot])
    if (data['phase']!='page-review-pair' or view.get('contract')!=CONTRACT
            or canonical.get('review_page_id')!=page['page_id'] or fingerprint(canonical)!=r['input_sha256']
            or canonical.get('review_revision_id')!=page['revision_id']
            or hashlib.sha256(canonical['content'].encode()).hexdigest()!=page['authored_sha256']
            or audit.get('page_request_id')!=r['request_id'] or audit['input_sha256']!=fingerprint(wire)
            or audit['schema_sha256']!=fingerprint(q['schema'])):fail('review slot input or native audit changed')
    response=entry.get('response')
    if not response:fail('review response not durably referenced')
    response=payload(vault,root,{'ref':(root/response['ref']).relative_to(vault).as_posix(),'sha256':response['sha256']})
    if (response.get('metadata')!=audit['model'] or response.get('audit_ref')!=acceptance['review_audit_ref']
            or fingerprint(response['raw'])!=r['response_sha256'] or response.get('raw_sha256')!=r['response_sha256'] or audit['response_sha256']!=r['response_sha256']
            or decode(response['raw']).get('results',{}).get(slot)!=r['result']):fail('review conclusion differs from native response')
    return r
