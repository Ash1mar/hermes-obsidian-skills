"""Page-owned independent review reservations, returned-response replay and receipts."""
import copy,json
from contextlib import contextmanager
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path
from hermes_source_units.validation import fingerprint
from hermes_source_units.recovery_control import epoch
from hermes_source_units.review_receipts import CONTRACT,restore_page
from page_pair_calls import PageCalls,ACTIVE,key
from page_budget_store import operation,Snapshot
from review_pair_request import canonical,prepare,images_for

def enabled(adapter,binding):
    p=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}/presentation-contract.json'
    if not p.exists():return False
    return _load_json(p).get('review_call_contract')==CONTRACT

def pseudo(page,view):return {'identity':{'kind':'review','identity_key':page['page_id']},'path':view['path']}
def folder(adapter,binding):return adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}/review-calls'
def wire_view(q):
    from fixed_semantic_recovery import _request_text
    v=json.loads(_request_text(q['wire']['user']))['input']
    if v.get('contract')!=CONTRACT:raise ContractError('STALE_INPUT','$','review request contract changed')
    return v

def exact_request(q,by_key):
    members=[[m['page_key'],m['ordinal']] for m in q['members'].values()]
    identity={'contract':'durable-page-calls/v1','members':members,'wire':q['wire'],'schema':q['schema'],'epoch':q['epoch']}
    if q.get('image_inputs'):identity['image_inputs']=q['image_inputs']
    if fingerprint(identity)!=q['request_id']:raise ContractError('STALE_INPUT','$','review reservation identity changed')
    v=wire_view(q)
    if set(v['pages'])!=set(q['members']):raise ContractError('STALE_INPUT','$','review page slots changed')
    for slot,m in q['members'].items():
        if m['page_key'] not in by_key or restore_page(v['pages'][slot])!=by_key[m['page_key']][1]:
            raise ContractError('STALE_INPUT','$','review body/evidence/risks differ from original reservation')
    return v

def retain_controlled(q,by_key,controls):
    v=wire_view(q)
    for slot,m in q['members'].items():
        control=controls.get(m['page_key'])
        if not control:continue
        old=restore_page(v['pages'][slot])
        if old.get('review_page_id')!=control['page_id'] or old.get('review_revision_id')!=control['original_revision_id']:
            raise ContractError('STALE_INPUT','$','controlled revision cannot refresh or replace its original review history')
        by_key[m['page_key']]=({'identity':{'kind':'review','identity_key':control['page_id']},'path':old['path']},old)

def execute(adapter,binding,value,caller,model_call,attach_receipt,committed):
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_recovery import native_reply
    from semantic_output import check,path_for
    from finalize_revision import work_for
    from hermes_source_units.workflow_guard import worker_binding,workflow_write_guard
    budget=value['backend'].get('input_budget')
    if not budget:raise ContractError('INPUT_BUDGET_CHANGED','$','new review protocol requires its fixed stage budget')
    checked=adapter.worker_check(binding)
    def guard():
        control=getattr(adapter,'worker_control_check',None)
        control(binding,checked=checked) if control else adapter.worker_check(binding)
        return epoch(adapter.workflow.vault,binding['workflow_id'])
    @contextmanager
    def transaction():
        with worker_binding(binding),workflow_write_guard(adapter.workflow.vault,kinds=('build-finalize',),actor=binding['actor']):yield
    root=folder(adapter,binding);book=PageCalls(root,guard,transaction,budget=budget,protocol='page-budget-index/v2')
    items=[];aliases={};outcomes={};canonical_views={};controls={}
    for view in value['views']:
        meta=value['backend']['pages'][view['page']];page=meta['page'];assessment=meta['assessment']
        if not assessment['risk_reasons']:continue
        controlled=work_for(adapter,binding,value,view)
        if controlled and controlled.get('grants'):
            controls[fingerprint({'kind':'review','identity_key':page['page_id']})]=controlled
            continue  # Separate explicitly authorized revision contract.
        from fixed_page_evidence import review_variants
        if any(path_for(adapter,binding,'page-review',v).exists() for v in review_variants(view,assessment)):
            raise ContractError('REVIEW_PROTOCOL_CONFLICT','$','consumed legacy review cannot acquire a paired allowance')
        v=canonical(view,page['page_id'],assessment,page.get('revision_id'));p=pseudo(page,v);k=key(p)
        items.append((p,v));aliases[k]=view['page'];canonical_views[k]=view
    by_key={key(p):(p,v) for p,v in items}
    def images(q):
        v=wire_view(q);v['pages']={s:restore_page(p) for s,p in v['pages'].items()}
        found,_=images_for(adapter,v)
        if [{k:v for k,v in i.items() if k!='url'} for i in found]!=q.get('image_inputs',[]):
            raise ContractError('SOURCE_CHANGED','$','review images changed')
        return found
    def authenticate(q,response):
        exact_request(q,by_key);p=_vault_path(adapter.workflow.vault,response['audit_ref']);audit=_load_json(p)
        if audit.get('phase')!='page-review-pair' or audit.get('page_request_id')!=q['request_id'] or audit.get('model')!=response['metadata']:return False
        proof=native_reply(adapter,binding,p,q['wire'],q['schema'],images(q))
        return bool(proof and proof[0]==response['raw'])
    def persist(k,result,request,audit):
        meta=value['backend']['pages'][aliases[k]]
        return {'page_id':meta['page']['page_id'],'authored_sha256':meta['page']['authored_sha256'],'assessment':copy.deepcopy(meta['assessment'])}
    def receive(receipt):
        k=receipt['page_key'];alias=aliases[k]
        with book.scan() as scan:row=scan.index['pages'][k];a=row['attempts'][receipt['ordinal']-1];pointer=row.get('receipt') if receipt['state']=='valid' else a['failure_receipt']
        pointer={'ref':(root/pointer['ref']).relative_to(adapter.workflow.vault).as_posix(),'sha256':pointer['sha256']}
        error=None if receipt['state']=='valid' else ContractError(receipt['code'],'$',receipt['reason'])
        if error:error.semantic_audit_ref=receipt['audit_ref']
        outcomes[alias]={'view':canonical_views[k],'result':receipt.get('result'),'audit_ref':receipt['audit_ref'],
            'receipt':pointer,'error':error}
        attach_receipt(adapter,receipt['audit_ref'],{'ok':error is None,'validated':error is None,
            'state':'independent_review_slot','page_receipt_id':receipt['receipt_id'],'review_receipt':pointer,
            'page_key':k,'page_request_id':receipt['request_id']})
    def settle(request):
        return book.settle(request,lambda k,d:check('page-review',None,None,None,d),persist,authenticate,
            recovery_epoch=guard(),on_receipt=receive,only_pages=set(aliases))
    # Authenticate saved responses BEFORE considering a fresh reservation.
    with book.scan() as scan:
        requests=list(scan.index['requests'])
        for rid in requests:
            q=scan.request(rid);retain_controlled(q,by_key,controls);exact_request(q,by_key)
            if 'response' not in q:
                matches=[]
                for p in (root.parent/'semantic-calls').glob('*.json'):
                    a=_load_json(p)
                    if a.get('phase')=='page-review-pair' and a.get('page_request_id')==rid:
                        proof=native_reply(adapter,binding,p,q['wire'],q['schema'],images(q))
                        if proof:matches.append((p,a,proof[0]))
                if len(matches)==1:
                    p,a,raw=matches[0];book.capture(rid,raw,p.relative_to(adapter.workflow.vault).as_posix(),a['model'])
                else:
                    for member in q['members'].values():
                        k=member['page_key']
                        if k not in aliases:continue
                        outcomes[aliases[k]]={'view':canonical_views[k],'result':None,'audit_ref':None,'receipt':None,
                            'error':ContractError('SEMANTIC_OUTPUT_HELD','$','unknown review reservation retained; no resend or quarantine')}
                    continue
            settle(rid)
    def submit(group,prepared_group=None):
        view,sc,prepared,pictures,_=prepared_group or prepare(group,prepare_request_projection,adapter=adapter,budget=budget)
        if not prepared['budget_check']['fits']:
            if len(group)==2:
                for one in group:submit([one])
                return
            k=key(group[0][0]);outcomes[aliases[k]]={'view':canonical_views[k],'result':None,'audit_ref':None,'receipt':None,
                'error':ContractError('READING_WINDOW_OVERSIZE','$','complete independent review exceeds pinned input/output budget')};return
        rid=book.reserve(group,prepared['wire'],sc,pictures);token=ACTIVE.set((book,rid))
        try:model_call(adapter,binding,'page-review-pair',view,caller,budget['phases']['page-review-pair']['input_codepoints'],attempt_limit=1,_checked=checked)
        finally:ACTIVE.reset(token)
        settle(rid)
    # Partial failures consume only their own single remaining correction.
    for correction in (False,True):
        pending=[]
        with book.scan() as scan:
            for p,v in items:
                row=scan.row(key(p));attempts=row.get('attempts',[]) if row else []
                if row and row.get('receipt'):continue
                if not row and not correction:pending.append((p,v))
                elif attempts and len(attempts)==1 and attempts[-1].get('correctable') and correction:pending.append((p,v))
        while pending:
            group=[pending.pop(0)]
            prepared_group=None
            if pending and not correction:
                trial=prepare([*group,pending[0]],prepare_request_projection,adapter=adapter,budget=budget)
                if trial[2]['budget_check']['fits']:
                    group.append(pending.pop(0));prepared_group=trial
            submit(group,prepared_group)
    return outcomes

@operation()
def history(adapter,binding,value,prove):
    """Stopped read-only authentication; never settles or grants a new ordinal."""
    root=folder(adapter,binding)
    if not (root/'page-call-budget.json').exists():return set(),[]
    by_key={};controls={}
    for view in value['views']:
        from finalize_revision import work_for
        meta=value['backend']['pages'][view['page']];v=canonical(view,meta['page']['page_id'],meta['assessment'],meta['page'].get('revision_id'));p=pseudo(meta['page'],v)
        control=work_for(adapter,binding,value,view)
        if control and control.get('grants'):controls[key(p)]=control
        by_key[key(p)]=(p,v)
    audits=set();rows=[];scan=Snapshot(root)
    for rid in scan.index['requests']:
        q=scan.request(rid);retain_controlled(q,by_key,controls);wire=exact_request(q,by_key)
        if 'response' in q:
            response=q['response'];ap=_vault_path(adapter.workflow.vault,response['audit_ref']);a=_load_json(ap)
            source={**wire,'pages':{s:restore_page(p) for s,p in wire['pages'].items()}};images,_=images_for(adapter,source)
            native=prove(ap,q['wire'],q['schema'],images)
            if not native or native[0]!=response['raw'] or a.get('page_request_id')!=rid or a.get('phase')!='page-review-pair':
                raise ContractError('RECOVERY_NOT_ALLOWED','$','independent review response lacks native proof')
            audits.add(response['audit_ref'])
        for m in q['members'].values():
            row=scan.row(m['page_key']);a=row['attempts'][m['ordinal']-1]
            if a['request_id']!=rid:raise ContractError('STALE_INPUT','$','review allowance ownership changed')
            receipt=row.get('receipt') if a['state']=='valid' else a.get('failure_receipt')
            if receipt and (receipt['request_id']!=rid or receipt['input_sha256']!=fingerprint(by_key[m['page_key']][1])
                    or receipt['receipt_id']!=fingerprint({k:v for k,v in receipt.items() if k!='receipt_id'})):
                raise ContractError('STALE_INPUT','$','review receipt changed')
            rows.append({'journal_name':'independent-review:'+m['page_key'],'request_id':rid,'page_key':m['page_key'],
                'preserved_attempt_count':len(row['attempts']),'prior_attempts_sha256':fingerprint(row['attempts']),
                'decision':receipt.get('result',{}).get('decision') if receipt else None,'unknown':'response' not in q,'additional_calls':0})
    return audits,rows

def journal_evidence(adapter,binding,view):
    root=folder(adapter,binding)
    if not (root/'page-call-budget.json').exists():return None
    with operation():
        scan=Snapshot(root);matches=[(k,row) for k,row in scan.index['pages'].items() if row['path']==view['path']]
        if not matches:return None
        if len(matches)!=1:raise ContractError('STALE_INPUT','$','ambiguous review page identity')
        k,_=matches[0];row=scan.row(k);attempts=[]
        for a in row['attempts']:
            q=scan.request(a['request_id']);response=q.get('response');ref=response['audit_ref'] if response else None
            audit={'ref':ref,'sha256':__import__('hashlib').sha256(_vault_path(adapter.workflow.vault,ref).read_bytes()).hexdigest()} if ref else None
            attempts.append({'attempt_sha256':fingerprint(a),'state':a['state'],'audit':audit,
                **({'completion_state':'completion_unknown'} if not response else {})})
        return {'attempt_count':len(attempts),'attempts':attempts,'journal':None,'review_budget_contract':CONTRACT,
            'page_key':k,'input_sha256':row['input_sha256'],'reserved_ordinals_preserved':True}
