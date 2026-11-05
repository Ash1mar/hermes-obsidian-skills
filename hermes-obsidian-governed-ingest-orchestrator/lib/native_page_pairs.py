"""Native page-call orchestration using the page-owned reservation/receipt ledger."""
import copy,threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from contextlib import contextmanager
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint
from hermes_source_units.recovery_control import epoch
from page_pair_calls import PageCalls,ACTIVE,key,CONTRACT


def run_groups(groups,submit,capacity):
    """Use the existing configured model-call bound; no nested dispatch layer."""
    errors=[]
    with ThreadPoolExecutor(max_workers=capacity) as pool:
        futures=[pool.submit(copy_context().run,submit,group) for group in groups]
        for future in futures:
            try:future.result()
            except Exception as exc:errors.append(exc)
    if errors:raise errors[0]


def compatible(left,right):
    a,b=left[1],right[1]
    if a.get('existing_page') or b.get('existing_page'):return False
    if fingerprint(a.get('original_evidence'))!=fingerprint(b.get('original_evidence')):return False
    def scope(v):return {c.get('applicability') for c in v.get('candidates',[])}
    if not scope(a) or None in scope(a) or scope(a)!=scope(b):return False
    for v in (a,b):
        proof=v.get('evidence_selection',{})
        if (proof.get('contract')!='page-dependencies/v1' or not proof.get('provenance_valid') or
                not all(r.get('source_mapping_complete') for r in proof.get('readings',[])) or v.get('coverage_limitations') or
                proof.get('view_sha256')!=fingerprint(v.get('original_evidence')) or
                proof.get('candidates_sha256')!=fingerprint(v.get('candidates',[])) or
                proof.get('proof_sha256')!=fingerprint({k:x for k,x in proof.items() if k!='proof_sha256'})):
            return False
        if v.get('original_evidence',{}).get('qa'):return False
        for c in v.get('candidates',[]):
            if not c.get('evidence_qa') or any(q.get('qa')!='usable' or q.get('qa_note') for q in c['evidence_qa']):return False
        for t in v.get('original_evidence',{}).get('tasks',[]):
            if any(t.get(f) for f in ('limits','missing','truncated')):return False
            if any(m.get(f) for m in t['materials'] for f in ('asset','linked_assets','qa_ref','qa_qualification')):return False
    return True


def execute(adapter,binding,value,state,caller,model_call,attach_receipt,project,measure):
    """Return None only before any durable page budget exists, for legacy fallback."""
    from page_execution_diagnostics import observe
    if value['view'].get('page_call_contract')!=CONTRACT:
        observe(binding,'contract_not_pinned');return None
    from semantic_output import output_boundary,check
    from reduce_disposition import page_permit
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{binding['workflow_id']}"
    owners=threading.local()
    def current_owner():return getattr(owners,'adapter',adapter)
    def guard():
        current_owner().worker_check(binding)
        w=current_owner().workflow.status(binding['workflow_id'])
        if w.get('cancel_requested') or w.get('state') in ('cancelled','failed'):
            raise ContractError('WORKFLOW_STOPPED','$','page call cancelled')
        return epoch(adapter.workflow.vault,binding['workflow_id'])
    from hermes_source_units.workflow_guard import worker_binding,workflow_write_guard
    @contextmanager
    def transaction():
        with worker_binding(binding),workflow_write_guard(adapter.workflow.vault,kinds=('global-reduce',),actor=binding['actor']):
            yield
    book=PageCalls(root,guard,transaction)
    boundary,_=output_boundary(adapter,binding)
    legacy=[]
    for folder in ('output-attempts','semantic-calls'):
        for path in (root/folder).glob('*.json'):
            row=_load_json(path)
            if row.get('phase')=='page-write':legacy.append(path)
    if legacy:
        if book.path.exists():raise ContractError('SEMANTIC_OUTPUT_HELD','$','mixed page budget contracts; no new allowance')
        observe(binding,'legacy_page_history',history_count=len(legacy));return None
    items=[]
    for page in state['pages']:
        if boundary or page_permit(adapter,binding,page):
            if book.path.exists():raise ContractError('SEMANTIC_OUTPUT_HELD','$','durable page calls require exact supported continuation')
            observe(binding,'output_boundary_or_disposition',page_path=page['path']);return None
        view,n=measure(adapter,value,page,caller,project)
        if n>30000:
            if book.path.exists():raise ContractError('SEMANTIC_OUTPUT_HELD','$','changed page budget; retain existing outcomes')
            observe(binding,'complete_input_oversize',page_path=page['path'],input_codepoints=n);return None
        if any(m.get('asset') for t in view.get('original_evidence',{}).get('tasks',[]) for m in t['materials']):
            if book.path.exists():raise ContractError('SEMANTIC_OUTPUT_HELD','$','asset call cannot replace an existing text-only reservation')
            observe(binding,'asset_requires_single',page_path=page['path']);return None
        items.append((page,view))
    from fixed_semantic_schema import schema_for
    from fixed_semantic_recovery import native_reply
    from fixed_semantic import prepare_request_projection
    by_key={key(p):(p,v) for p,v in items};calls=[];written={};held=[]
    if len(by_key)!=len(items):raise ContractError('IDENTITY_CONFLICT','$','duplicate durable page identities')
    for page,view in items:book.status(page,view)  # Fail before persisting any changed-input result.
    def authenticate(q,response):
        ref=response['audit_ref'];path=_vault_path(adapter.workflow.vault,ref)
        if not path.is_file():return False
        audit=_load_json(path)
        if audit.get('page_request_id')!=q['request_id'] or audit.get('phase')!='page-pair' or audit.get('model')!=response['metadata']:return False
        proof=native_reply(current_owner(),binding,path,q['wire'],q['schema'],[])
        return bool(proof and proof[0]==response['raw'])
    def validate(k,draft):return check('page-write',value,None,None,draft)
    def persist(k,draft,request,audit):
        page,view=by_key[k];ref=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/page-drafts/{fingerprint(view)}.md"
        raw=draft['content'].encode('utf-8');p=_vault_path(adapter.workflow.vault,ref)
        if p.exists() and p.read_bytes()!=raw:raise ContractError('SOURCE_CHANGED','$','durable native page body changed')
        if not p.exists():_write_atomic(p,raw)
        return {'draft_ref':ref,'content_sha256':fingerprint(draft['content'])}
    def acknowledge(receipt):
        # Receipt was atomically published before this external, idempotent link.
        if receipt['state']!='valid':
            attach_receipt(current_owner(),receipt['audit_ref'],{'ok':False,'validated':False,'state':'page_output_rejected',
                'page_receipt_id':receipt['receipt_id'],'page_key':receipt['page_key'],'page_request_id':receipt['request_id'],
                'code':receipt['code'],'reason':receipt['reason']})
            return
        page,view=by_key[receipt['page_key']]
        draft_path=_vault_path(adapter.workflow.vault,receipt['saved']['draft_ref'])
        if draft_path.read_text(encoding='utf-8')!=receipt['result']['content']:
            raise ContractError('SOURCE_CHANGED','$','page receipt body changed')
        attach_receipt(current_owner(),receipt['audit_ref'],{'ok':True,'validated':True,'state':'page_body_only',
            'page_receipt_id':receipt['receipt_id'],'page_key':receipt['page_key'],'page_request_id':receipt['request_id'],**receipt['saved']})
        written[receipt['page_key']]={**page,'content':receipt['result']['content'],'draft_ref':receipt['saved']['draft_ref'],
            'audit_ref':receipt['audit_ref'],'input_sha256':fingerprint(view)}
        if receipt['audit_ref'] not in calls:calls.append(receipt['audit_ref'])
    def settle(request):
        book.settle(request,validate,persist,authenticate,on_receipt=acknowledge)
    # Saved raw responses settle pages one at a time; no model call on this path.
    for request,q in book.read()['requests'].items():
        if not set(m['page_key'] for m in q['members'].values())<=set(by_key):
            raise ContractError('STALE_INPUT','$','page plan changed after durable reservation')
        if 'response' not in q:
            matches=[]
            for p in (root/'semantic-calls').glob('*.json'):
                audit=_load_json(p)
                if audit.get('page_request_id')==request:
                    proof=native_reply(adapter,binding,p,q['wire'],q['schema'],[])
                    if proof:matches.append((p,audit,proof[0]))
            if len(matches)!=1:raise ContractError('SEMANTIC_OUTPUT_HELD','$','unknown page request; no automatic resend')
            p,audit,raw=matches[0];book.capture(request,raw,p.relative_to(adapter.workflow.vault).as_posix(),audit['model'])
        settle(request)
    def submit(group):
        from fixed_semantic import HermesCaller
        from ingest_kanban import IngestKanbanAdapter
        local_caller=caller.fork() if isinstance(caller,HermesCaller) else caller
        owners.adapter=(IngestKanbanAdapter(adapter.workflow.vault,enable_workers=adapter.enable_workers)
                        if isinstance(adapter,IngestKanbanAdapter) else adapter)
        # Pair only when originals (including local material handles) are identical.
        evidence=copy.deepcopy(group[0][1].get('original_evidence',{}))
        pages={f'p{i}':{k:copy.deepcopy(v) for k,v in view.items() if k not in ('original_evidence','evidence_encoding')} for i,(p,view) in enumerate(group,1)}
        wire_view={'contract':CONTRACT,'pages':pages,'original_evidence':evidence,
                   'page_members':{f'p{i}':key(p) for i,(p,v) in enumerate(group,1)}}
        diagnostics={}
        for i,(page,view) in enumerate(group,1):
            prior=book.status(page,view)
            if prior and prior['attempts']:
                last=prior['attempts'][-1];diagnostics[f'p{i}']={'code':last.get('code'),'reason':last.get('reason')}
        if diagnostics:wire_view['page_validation_diagnostics']=diagnostics
        sc=schema_for('page-pair',wire_view);prepared=prepare_request_projection('page-pair',wire_view,sc,[])
        if prepared['input_size']>30000:
            if len(group)==2:
                for item in group:submit([item])
                return
            raise ContractError('READING_WINDOW_OVERSIZE','$','complete durable page request exceeds bound')
        request=book.reserve(group,prepared['wire'],sc)
        token=ACTIVE.set((book,request))
        try:model_call(current_owner(),binding,'page-pair',wire_view,local_caller,30000,attempt_limit=1)
        finally:ACTIVE.reset(token)
        settle(request)
    # Only new or rejected mechanical outputs can request a call. Completed pages
    # are immutable and never enter another group, including after restart.
    pending=[]
    for page,view in items:
        row=book.status(page,view)
        if row and row.get('receipt'):acknowledge(row['receipt']);continue
        if row and (len(row['attempts'])>=2 or not row['attempts'][-1].get('correctable')):continue
        pending.append((page,view))
    groups=[]
    while pending:
        first=pending.pop(0);group=[first]
        # Additional checks are isolated to the failed page, never the valid peer.
        if pending and not book.status(*first) and not book.status(*pending[0]) and compatible(first,pending[0]):group.append(pending.pop(0))
        groups.append(group)
    capacity=min(10,getattr(adapter,'reduce_page_concurrency',2))
    run_groups(groups,submit,capacity)
    corrections=[]
    # One bounded correction for remaining mechanical failures, sharing the ledger.
    for page,view in items:
        row=book.status(page,view)
        if row and not row.get('receipt') and len(row['attempts'])==1 and row['attempts'][0].get('correctable'):
            corrections.append([(page,view)])
    run_groups(corrections,submit,capacity)
    for page,view in items:
        row=book.status(page,view)
        if row and row.get('receipt'):acknowledge(row['receipt']);continue
        last=row['attempts'][-1] if row and row['attempts'] else {}
        held.append({'path':page['path'],'candidate_refs':page['candidate_refs'],'phase':'page-write',
            'code':'SEMANTIC_REVIEW_REQUIRED' if last.get('state')=='rejected' else 'SEMANTIC_OUTPUT_HELD','validation_code':last.get('code'),'reason':last.get('reason','page outcome pending'),
            'audit_ref':book.read()['requests'][last['request_id']].get('response',{}).get('audit_ref') if last else None})
    working=copy.deepcopy(state)
    from qa_program import page_link_failures
    held.extend(page_link_failures(value,working,list(written.values())))
    if held:
        from qa_program import isolate_pages
        outcome=isolate_pages(adapter,binding,value,working,list(written.values()),held,None)
        if not outcome:raise ContractError('SEMANTIC_REVIEW_REQUIRED','$','durable pages retained; unresolved pages held')
        working,_=outcome
    import fixed_global_packets as g
    result=g.complete(value,working);bodies={p['path']:p['content'] for p in written.values()}
    for page in result['pages']:page['content']=bodies[page['path']]
    return result,calls
