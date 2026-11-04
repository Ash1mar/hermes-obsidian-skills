"""Atomic page-owned budgets and independent receipts for one/two-slot requests.

One write publishes a reservation for every page. Returned raw wire is durable
before settlement; each page/receipt is committed separately under the same lock.
Unknown reservations never authorize resend. Group identities cannot mint budget.
"""
import copy,json
from contextvars import ContextVar
from contextlib import nullcontext
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_write_atomic,_json_bytes,_exclusive_lock
from hermes_source_units.validation import fingerprint

CONTRACT='durable-page-calls/v1'
ACTIVE=ContextVar('durable_page_request',default=None)

def fail(code,message):raise ContractError(code,'$',message)
def key(page):return fingerprint({'kind':page['identity']['kind'],'identity_key':page['identity']['identity_key']})

class PageCalls:
    def __init__(self,root,guard,transaction=nullcontext):
        self.transaction=transaction
        self.root=root;self.path=root/'page-call-budget.json';self.guard=guard
    def read(self):
        record=_load_json(self.path) if self.path.exists() else {'contract':CONTRACT,'pages':{},'requests':{}}
        if record.get('contract')!=CONTRACT:fail('STALE_INPUT','unknown page budget contract')
        return record
    def save(self,r):_write_atomic(self.path,_json_bytes(r))
    def status(self,page,view):
        r=self.read();row=r['pages'].get(key(page))
        if row and row['input_sha256']!=fingerprint(view):fail('STALE_INPUT','page inputs changed; budget cannot reset')
        return copy.deepcopy(row)
    def reserve(self,items,wire,schema):
        if not 1<=len(items)<=2 or len({key(p) for p,v in items})!=len(items):fail('INVALID_SCHEMA','one or two unique page identities required')
        with self.transaction(), _exclusive_lock(self.path.with_suffix('.lock'),timeout=5):
            epoch=self.guard();r=self.read();members=[]
            for page,view in items:
                k=key(page);row=r['pages'].get(k)
                if row:
                    if row['input_sha256']!=fingerprint(view) or row['path']!=page['path']:fail('STALE_INPUT','page identity/input changed')
                    if row.get('receipt'):fail('INVALID_TRANSITION','valid page must be reused, not reserved')
                    if row['attempts'] and row['attempts'][-1]['state']=='reserved':fail('SEMANTIC_OUTPUT_HELD','unknown page call; no automatic resend')
                    if len(row['attempts'])>=2:fail('SEMANTIC_OUTPUT_HELD','page additional-check budget exhausted')
                    if row['attempts'] and not row['attempts'][-1].get('correctable'):fail('SEMANTIC_REVIEW_REQUIRED','evidence block is not correctable')
                else:row={'path':page['path'],'identity':copy.deepcopy(page['identity']),'input_sha256':fingerprint(view),'attempts':[]};r['pages'][k]=row
                members.append([k,len(row['attempts'])+1])
            request=fingerprint({'contract':CONTRACT,'members':members,'wire':wire,'schema':schema,'epoch':epoch})
            record={'request_id':request,'epoch':epoch,'members':{f'p{i}':{'page_key':k,'ordinal':n} for i,(k,n) in enumerate(members,1)},
                    'state':'reserved','wire':copy.deepcopy(wire),'schema':copy.deepcopy(schema)}
            if request in r['requests']:fail('INVALID_TRANSITION','request already reserved')
            r['requests'][request]=record
            for k,n in members:r['pages'][k]['attempts'].append({'ordinal':n,'request_id':request,'state':'reserved'})
            if self.guard()!=epoch:fail('WORKFLOW_STOPPED','cancellation epoch changed before reservation')
            self.save(r);return request
    def capture(self,request,raw,audit_ref,metadata):
        # Retaining a returned response after cancellation grants no execution.
        with _exclusive_lock(self.path.with_suffix('.lock'),timeout=5):
            r=self.read();q=r['requests'][request]
            value={'raw':raw,'raw_sha256':fingerprint(raw),'audit_ref':audit_ref,'metadata':copy.deepcopy(metadata)}
            if 'response' in q:
                if q['response']!=value:fail('SOURCE_CHANGED','returned response changed')
                return
            q['response']=value;q['state']='returned';self.save(r)
    def settle(self,request,validate,persist,authenticate,*,after_page=None,on_receipt=None):
        q=self.read()['requests'][request]
        members=[[m['page_key'],m['ordinal']] for m in q['members'].values()]
        if fingerprint({'contract':CONTRACT,'members':members,'wire':q['wire'],'schema':q['schema'],'epoch':q['epoch']})!=request:
            fail('SOURCE_CHANGED','request member/wire identity changed')
        if 'response' not in q:fail('SEMANTIC_OUTPUT_HELD','unknown request; reservation retained')
        response=q['response']
        if fingerprint(response['raw'])!=response['raw_sha256'] or not authenticate(q,response):fail('SOURCE_CHANGED','response lacks exact native proof')
        from fixed_semantic import decode_semantic_response
        try:
            decoded,_=decode_semantic_response(response['raw']);slots=decoded.get('results',{}) if isinstance(decoded,dict) else {}
        except (ValueError,TypeError):slots={}
        if not isinstance(slots,dict) or set(slots)-set(q['members']):slots={}
        results={}
        for slot,member in q['members'].items():
            with self.transaction(), _exclusive_lock(self.path.with_suffix('.lock'),timeout=5):
                if self.guard()!=q['epoch']:fail('WORKFLOW_STOPPED','cancel superseded page settlement')
                r=self.read();page=r['pages'][member['page_key']];attempt=page['attempts'][member['ordinal']-1]
                if attempt['state']=='rejected':
                    results[member['page_key']]=copy.deepcopy(attempt['failure_receipt'])
                    if on_receipt:on_receipt(results[member['page_key']])
                    continue
                if page.get('receipt'):
                    receipt=page['receipt']
                    if (receipt['request_id']!=request or receipt['page_key']!=member['page_key'] or
                            receipt['response_sha256']!=response['raw_sha256'] or
                            receipt['receipt_id']!=fingerprint({k:v for k,v in receipt.items() if k!='receipt_id'})):
                        fail('STALE_INPUT','different or damaged page receipt')
                    results[member['page_key']]=copy.deepcopy(receipt)
                    if on_receipt:on_receipt(results[member['page_key']])
                    continue
                draft=slots.get(slot)
                try:value=validate(member['page_key'],draft)
                except ContractError as exc:
                    correctable=exc.code in {'INVALID_SCHEMA','INCOMPLETE_COVERAGE','DUPLICATE','UNRESOLVED_REFERENCE','IDENTITY_CONFLICT'}
                    attempt.update(state='rejected',correctable=correctable,code=exc.code,reason=str(exc))
                    receipt={'state':'rejected','page_key':member['page_key'],'request_id':request,'ordinal':member['ordinal'],
                             'input_sha256':page['input_sha256'],'response_sha256':response['raw_sha256'],
                             'audit_ref':response['audit_ref'],'correctable':correctable,'code':exc.code,'reason':str(exc)}
                    receipt['receipt_id']=fingerprint(receipt);attempt['failure_receipt']=receipt;self.save(r)
                    results[member['page_key']]=copy.deepcopy(receipt)
                else:
                    # Deterministic persist is idempotent if interrupted before receipt.
                    saved=persist(member['page_key'],value,request,response['audit_ref'])
                    if self.guard()!=q['epoch']:fail('WORKFLOW_STOPPED','cancel before page receipt')
                    receipt={'state':'valid','page_key':member['page_key'],'request_id':request,'ordinal':member['ordinal'],
                             'input_sha256':page['input_sha256'],'response_sha256':response['raw_sha256'],
                             'audit_ref':response['audit_ref'],'result':value,'saved':saved}
                    receipt['receipt_id']=fingerprint(receipt);page['receipt']=receipt;attempt['state']='valid';self.save(r)
                    results[member['page_key']]=copy.deepcopy(receipt)
            if on_receipt:on_receipt(results[member['page_key']])
            if after_page:after_page(member['page_key'])
        return results

def capture(adapter,binding,raw,ref,metadata):
    active=ACTIVE.get()
    if active:
        book,request=active;book.capture(request,raw,ref,metadata)
