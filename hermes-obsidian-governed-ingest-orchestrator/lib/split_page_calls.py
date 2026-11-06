"""Transaction-fresh page reservations with independently durable slot receipts."""
import copy,threading
from contextlib import nullcontext,contextmanager
from hermes_source_units import ContractError
from hermes_source_units.source_units import _write_atomic,_json_bytes,_exclusive_lock
from hermes_source_units.validation import fingerprint
from page_budget_store import INDEX,WIRE,Snapshot,load_index,put,orphan,payload,operation,fail

def deny(code,message):raise ContractError(code,'$',message)
def page_key(page):return fingerprint({'kind':page['identity']['kind'],'identity_key':page['identity']['identity_key']})

class SplitPageCalls:
    storage_contract=INDEX
    def __init__(self,root,guard,transaction=nullcontext,*,budget=None,**_):
        self.root=root;self.path=root/'page-call-budget.json';self.guard=guard
        self.transaction=transaction;self.mutex=threading.RLock()
        self.budget=copy.deepcopy(budget)
    @contextmanager
    def scan(self):
        with operation() as stats:yield Snapshot(self.root)
    def read(self):
        with operation():return Snapshot(self.root).full()
    def save(self,r):
        # Compatibility inspection/fault-injection API; production mutations use
        # only fresh small indexes and immutable writes below.
        current=load_index(self.root,fresh=True)
        if current['contract']==WIRE:_write_atomic(self.path,_json_bytes(r));return
        if set(r['pages'])!=set(current['pages']) or set(r['requests'])!=set(current['requests']):deny('STALE_INPUT','inspection cannot add or remove budget identities')
        for k,row in r['pages'].items():
            prior=current['pages'][k]
            if (row['input_sha256']!=prior['input_sha256'] or row['path']!=prior['path']
                    or bool(row.get('receipt'))!=bool(prior.get('receipt'))
                    or [{f:a.get(f) for f in ('ordinal','request_id','state','correctable')} for a in row['attempts']]
                    !=[{f:a.get(f) for f in ('ordinal','request_id','state','correctable')} for a in prior['attempts']]):deny('STALE_INPUT','inspection cannot refresh consumed page allowance')
        index={**{k:copy.deepcopy(v) for k,v in current.items() if k not in ('pages','requests')},'pages':{},'requests':{}}
        for k,row in r['pages'].items():
            small=copy.deepcopy(row)
            if small.get('receipt'):small['receipt']=put(self.root,'inspection-receipts',fingerprint(row['receipt']),row['receipt'])
            for a in small['attempts']:
                if a.get('failure_receipt'):a['failure_receipt']=put(self.root,'inspection-receipts',fingerprint(a['failure_receipt']),a['failure_receipt'])
            index['pages'][k]=small
        for k,q in r['requests'].items():
            body={f:v for f,v in q.items() if f not in ('response','state')}
            small={f:q[f] for f in ('request_id','epoch','members','state')}
            small['payload']=put(self.root,'inspection-requests',fingerprint(body),body)
            if 'response' in q:small['response']=put(self.root,'inspection-responses',fingerprint(q['response']),q['response'])
            index['requests'][k]=small
        self._save(index)
    def _save(self,index):
        from page_budget_store import publish_index
        index['version']+=1;raw=_json_bytes(index);_write_atomic(self.path,raw)
        publish_index(self.root,index,raw)
    def status(self,page,view):
        with self.scan() as scan:row=scan.row(page_key(page))
        if row and row['input_sha256']!=fingerprint(view):deny('STALE_INPUT','page inputs changed; budget cannot reset')
        return copy.deepcopy(row)
    def reserve(self,items,wire,schema,images=()):
        if not 1<=len(items)<=2 or len({page_key(p) for p,v in items})!=len(items):deny('INVALID_SCHEMA','one or two unique pages required')
        with self.mutex,self.transaction(),_exclusive_lock(self.path.with_suffix('.lock'),timeout=5):
            epoch=self.guard();r=load_index(self.root,fresh=True);members=[]
            if r['contract']!=INDEX:deny('PAGE_PROTOCOL_UNSUPPORTED','new reservation cannot migrate historical budget')
            if self.budget:
                if r.get('input_budget')!=self.budget and r['requests']:deny('INPUT_BUDGET_CHANGED','consumed page budget cannot switch profiles')
                r['input_budget']=copy.deepcopy(self.budget)
            elif r.get('input_budget'):deny('INPUT_BUDGET_CHANGED','pinned page budget must be supplied for reservation')
            for page,view in items:
                k=page_key(page);row=r['pages'].get(k)
                if row:
                    if row['input_sha256']!=fingerprint(view) or row['path']!=page['path']:deny('STALE_INPUT','page identity/input changed')
                    if row.get('receipt'):deny('INVALID_TRANSITION','valid page must be reused, not reserved')
                    if row['attempts'] and row['attempts'][-1]['state']=='reserved':deny('SEMANTIC_OUTPUT_HELD','unknown page call; no automatic resend')
                    if len(row['attempts'])>=2:deny('SEMANTIC_OUTPUT_HELD','page additional-check budget exhausted')
                    if row['attempts'] and not row['attempts'][-1].get('correctable'):deny('SEMANTIC_REVIEW_REQUIRED','evidence block is not correctable')
                else:
                    row={'path':page['path'],'identity':copy.deepcopy(page['identity']),'input_sha256':fingerprint(view),'attempts':[]};r['pages'][k]=row
                members.append([k,len(row['attempts'])+1])
            identity={'contract':WIRE,'members':members,'wire':wire,'schema':schema,'epoch':epoch}
            if images:identity['image_inputs']=[{k:v for k,v in i.items() if k!='url'} for i in images]
            request=fingerprint(identity)
            body={'request_id':request,'epoch':epoch,'members':{f'p{i}':{'page_key':k,'ordinal':n} for i,(k,n) in enumerate(members,1)},'wire':copy.deepcopy(wire),'schema':copy.deepcopy(schema)}
            if images:body['image_inputs']=identity['image_inputs']
            if request in r['requests']:deny('INVALID_TRANSITION','request already reserved')
            r['requests'][request]={**{k:body[k] for k in ('request_id','epoch','members')},'state':'reserved','payload':put(self.root,'requests',request,body)}
            for k,n in members:r['pages'][k]['attempts'].append({'ordinal':n,'request_id':request,'state':'reserved'})
            if self.guard()!=epoch:deny('WORKFLOW_STOPPED','cancel epoch changed before reservation')
            self._save(r);return request
    def claim_transport_retry(self,request,permit,owner):
        """Reuse an authenticated unknown reservation, without another page ordinal."""
        with self.mutex,self.transaction(),_exclusive_lock(self.path.with_suffix('.lock'),timeout=5),operation():
            epoch=self.guard();r=load_index(self.root,fresh=True);small=r['requests'][request]
            q=Snapshot(self.root,r).request(request)
            if 'response' in q:deny('INVALID_TRANSITION','returned request must settle, never resend')
            if (permit['request_id']!=request or permit['members']!=q['members']
                    or permit['wire_sha256']!=fingerprint(q['wire']) or permit['schema_sha256']!=fingerprint(q['schema'])):
                deny('STALE_INPUT','transport retry request changed')
            for m in q['members'].values():
                p=r['pages'][m['page_key']];proof=permit['page_reservations'][m['page_key']]
                if (p.get('receipt') or p['input_sha256']!=proof['input_sha256'] or p['path']!=proof['path']
                        or fingerprint(p['attempts'])!=proof['attempts_sha256']
                        or p['attempts'][-1]!={'ordinal':m['ordinal'],'request_id':request,'state':'reserved'}):
                    deny('STALE_INPUT','valid or changed page cannot be retried')
            grant=fingerprint(permit)
            if any(a['grant_sha256']==grant for a in small.get('transport_retries',[])):
                deny('SEMANTIC_OUTPUT_HELD','transport grant already dispatched; recover its outcome first')
            small.setdefault('transport_retries',[]).append({'grant_sha256':grant,'recovery_id':permit['recovery_id'],
                'failure_ref':permit['failure_ref'],'failure_sha256':permit['failure_sha256'],
                'owner':copy.deepcopy(owner),'state':'dispatched','new_semantic_ordinals':0})
            if self.guard()!=epoch:deny('WORKFLOW_STOPPED','stop changed before transport retry')
            self._save(r)
    def capture(self,request,raw,audit_ref,metadata):
        with self.mutex,_exclusive_lock(self.path.with_suffix('.lock'),timeout=5),operation():
            r=load_index(self.root,fresh=True);q=r['requests'][request]
            value={'raw':raw,'raw_sha256':fingerprint(raw),'audit_ref':audit_ref,'metadata':copy.deepcopy(metadata)}
            pointer=put(self.root,'responses',request,value)
            if q.get('response') and q['response']!=pointer:fail('returned response changed')
            q['response']=pointer;q['state']='returned'
            if q.get('transport_retries'):q['transport_retries'][-1].update(state='returned',audit_ref=audit_ref)
            self._save(r)
    def settle(self,request,validate,persist,authenticate,*,after_page=None,on_receipt=None,recovery_epoch=None,only_pages=None):
        with operation():
            q=Snapshot(self.root,load_index(self.root,fresh=True)).request(request)
            members=[[m['page_key'],m['ordinal']] for m in q['members'].values()]
            identity={'contract':WIRE,'members':members,'wire':q['wire'],'schema':q['schema'],'epoch':q['epoch']}
            if 'image_inputs' in q:identity['image_inputs']=q['image_inputs']
            if fingerprint(identity)!=request:fail('request member/wire identity changed')
            if 'response' not in q:deny('SEMANTIC_OUTPUT_HELD','unknown request; reservation retained')
            response=q['response']
            if fingerprint(response['raw'])!=response['raw_sha256'] or not authenticate(q,response):fail('response lacks exact native proof')
            epoch=q['epoch'] if recovery_epoch is None else recovery_epoch
            from fixed_semantic import decode_semantic_response
            try:
                decoded,_=decode_semantic_response(response['raw']);slots=decoded.get('results',{}) if isinstance(decoded,dict) else {}
            except (ValueError,TypeError):slots={}
            if not isinstance(slots,dict) or set(slots)-set(q['members']):slots={}
            results={}
            for slot,member in q['members'].items():
                k=member['page_key'];ordinal=member['ordinal'];receipt_key=f'{k}-{ordinal}-{request}'
                if only_pages is not None and k not in only_pages:continue
                with self.mutex,self.transaction(),_exclusive_lock(self.path.with_suffix('.lock'),timeout=5):
                    if self.guard()!=epoch:deny('WORKFLOW_STOPPED','cancel superseded page settlement')
                    r=load_index(self.root,fresh=True);page=r['pages'][k];attempt=page['attempts'][ordinal-1]
                    changed=False
                    if not r['requests'][request].get('response'):
                        r['requests'][request]['response']=orphan(self.root,'responses',request)
                        r['requests'][request]['state']='returned';changed=True
                    if attempt['request_id']!=request:fail('page attempt request mapping changed')
                    pointer=attempt.get('failure_receipt') or page.get('receipt') or orphan(self.root,'receipts',receipt_key)
                    if pointer:
                        receipt=payload(self.root,pointer)
                        if (receipt['request_id']!=request or receipt['page_key']!=k or receipt['ordinal']!=ordinal
                                or receipt['input_sha256']!=page['input_sha256']
                                or receipt['response_sha256']!=response['raw_sha256']
                                or receipt['receipt_id']!=fingerprint({a:b for a,b in receipt.items() if a!='receipt_id'})):fail('different or damaged page receipt')
                        if receipt['state']=='valid' and receipt['result']!=slots.get(slot):fail('receipt differs from native result slot')
                    else:
                        draft=slots.get(slot)
                        try:value=validate(k,draft)
                        except ContractError as exc:
                            correctable=exc.code in {'INVALID_SCHEMA','INCOMPLETE_COVERAGE','DUPLICATE','UNRESOLVED_REFERENCE','IDENTITY_CONFLICT'}
                            receipt={'state':'rejected','correctable':correctable,'code':exc.code,'reason':str(exc)}
                        else:
                            saved=persist(k,value,request,response['audit_ref'])
                            if self.guard()!=epoch:deny('WORKFLOW_STOPPED','cancel before page receipt')
                            receipt={'state':'valid','result':value,'saved':saved}
                        receipt.update(page_key=k,request_id=request,ordinal=ordinal,input_sha256=page['input_sha256'],response_sha256=response['raw_sha256'],audit_ref=response['audit_ref'])
                        receipt['receipt_id']=fingerprint(receipt);pointer=put(self.root,'receipts',receipt_key,receipt)
                    if self.guard()!=epoch:deny('WORKFLOW_STOPPED','cancel before settlement commit')
                    if receipt['state']=='valid':
                        changed=changed or page.get('receipt')!=pointer or attempt['state']!='valid'
                        page['receipt']=pointer;attempt['state']='valid'
                    else:
                        changed=changed or attempt.get('failure_receipt')!=pointer or attempt['state']!='rejected'
                        attempt.update(state='rejected',failure_receipt=pointer,**{a:receipt[a] for a in ('correctable','code','reason')})
                    if changed:self._save(r)
                    results[k]=copy.deepcopy(receipt)
                if on_receipt:on_receipt(results[k])
                if after_page:after_page(k)
            return results
