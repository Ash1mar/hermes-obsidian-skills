"""Read-only native proof for page-owned receipts and stopped recovery.

This authenticates returned outputs only. It never settles, grants, reserves or
dispatches a model call. The caller owns current admission/ended-run authority.
"""
import copy,json
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path
from hermes_source_units.validation import fingerprint
from page_pair_calls import key,CONTRACT

def fail(message):raise ContractError('SEMANTIC_OUTPUT_HELD','$',message)

def authenticate_request(adapter,binding,value,request,prove):
    from fixed_semantic_recovery import _request_text
    from fixed_page_evidence import restore_pair
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_schema import schema_for
    from fixed_semantic_media import image_catalog,select_images
    import fixed_reduce
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}'
    ledger=_load_json(root/'page-call-budget.json')
    if ledger.get('contract')!=CONTRACT:fail('unknown page receipt contract')
    q=ledger['requests'][request];members=[[m['page_key'],m['ordinal']] for m in q['members'].values()]
    identity={'contract':CONTRACT,'members':members,'wire':q['wire'],'schema':q['schema'],'epoch':q['epoch']}
    if 'image_inputs' in q:identity['image_inputs']=q['image_inputs']
    if not 1<=len(members)<=2 or q['request_id']!=request or fingerprint(identity)!=request:fail('request member/wire identity changed')
    if 'response' not in q:fail('unknown page response cannot be recovered as QA')
    wire_view=json.loads(_request_text(q['wire']['user']))['input']
    if wire_view.get('evidence_encoding')=='lossless-page-pair/v1':wire_view=restore_pair(wire_view)
    if set(wire_view['pages'])!=set(q['members']):fail('request slots changed')
    for slot,member in q['members'].items():
        canonical={**wire_view['pages'][slot],'original_evidence':wire_view['original_evidence']}
        page={'identity':canonical['identity'],'path':canonical['path'],
            'candidate_refs':[c['candidate'] for c in canonical['candidates']]}
        if member['page_key']!=key(page) or wire_view['page_members'][slot]!=member['page_key']:fail('page slot identity changed')
        current=fixed_reduce.page_view(adapter,value,page)
        if canonical.get('page_scope_contract'):current['page_scope_contract']=canonical['page_scope_contract']
        row=ledger['pages'][member['page_key']]
        if current!=canonical or row['input_sha256']!=fingerprint(canonical) or row['path']!=page['path']:fail('page evidence differs from current snapshot')
        if row['attempts'][member['ordinal']-1]['request_id']!=request:fail('page ordinal mapping changed')
    evidence=wire_view['original_evidence'];images=[]
    if q.get('image_inputs'):
        images=select_images(evidence['tasks'],image_catalog(adapter,evidence))
        if [{k:v for k,v in i.items() if k!='url'} for i in images]!=q['image_inputs']:fail('page image identity changed')
    sc=schema_for('page-pair',wire_view)
    if sc!=q['schema'] or prepare_request_projection('page-pair',wire_view,sc,images)['wire']!=q['wire']:fail('page request no longer reconstructs exactly')
    response=q['response'];path=_vault_path(adapter.workflow.vault,response['audit_ref']);audit=_load_json(path)
    if (audit.get('page_request_id')!=request or audit.get('phase')!='page-pair' or audit.get('model')!=response['metadata']
            or fingerprint(response['raw'])!=response['raw_sha256']):fail('page audit or raw response changed')
    native=prove(path,q['wire'],q['schema'],images)
    if not native or native[0]!=response['raw']:fail('page response lacks exact ended native proof')
    return ledger,q,wire_view

def read_outcome(adapter,binding,view,prove,images,*,value=None):
    from semantic_output import check
    from fixed_semantic import decode_semantic_response
    root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}'
    path=root/'page-call-budget.json'
    if not path.exists():return None
    canonical={k:v for k,v in view.items() if k!='evidence_encoding'};ledger=_load_json(path)
    k=key({'identity':canonical['identity']});row=ledger['pages'].get(k)
    if row is None:return None
    if row['input_sha256']!=fingerprint(canonical) or not 1<=len(row['attempts'])<=2:fail('page input or allowance changed')
    last=None
    # The current source snapshot is supplied by this stopped/worker caller.
    from fixed_reduce import preview_snapshot
    if value is None:value=preview_snapshot(adapter,binding)
    for ordinal,attempt in enumerate(row['attempts'],1):
        if attempt['ordinal']!=ordinal:fail('page allowance order changed')
        ledger,q,wire=authenticate_request(adapter,binding,value,attempt['request_id'],prove)
        slot=next(s for s,m in q['members'].items() if m['page_key']==k and m['ordinal']==ordinal)
        if {**wire['pages'][slot],'original_evidence':wire['original_evidence']}!=canonical:fail('receipt input differs')
        decoded,_=decode_semantic_response(q['response']['raw']);draft=decoded.get('results',{}).get(slot)
        error=None
        try:check('page-write',None,None,None,draft)
        except ContractError as exc:error=exc
        if error is None:
            r=row.get('receipt')
            if (attempt['state']!='valid' or ordinal!=len(row['attempts']) or not r or r['result']!=draft
                    or r['request_id']!=attempt['request_id'] or r['input_sha256']!=row['input_sha256']
                    or r['response_sha256']!=q['response']['raw_sha256']
                    or r['receipt_id']!=fingerprint({k:v for k,v in r.items() if k!='receipt_id'})):fail('valid page receipt changed')
            expected=f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}/page-drafts/{fingerprint(canonical)}.md'
            if r['saved']['draft_ref']!=expected or _vault_path(adapter.workflow.vault,expected).read_text()!=draft['content']:fail('saved page body changed')
            last={'kind':'valid','ref':r['audit_ref'],'draft':draft,'error':None}
        else:
            r=attempt.get('failure_receipt',{})
            if attempt['state']!='rejected' or attempt.get('code')!=error.code or r.get('receipt_id')!=fingerprint({k:v for k,v in r.items() if k!='receipt_id'}):fail('rejected page receipt changed')
            if ordinal<len(row['attempts']) and not attempt.get('correctable'):fail('non-correctable page gained allowance')
            last={'kind':'held','ref':q['response']['audit_ref'],'error':error}
    return last
