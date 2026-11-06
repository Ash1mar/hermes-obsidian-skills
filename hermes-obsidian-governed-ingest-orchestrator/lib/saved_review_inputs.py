"""Read-only native input authentication and complete-request review comparisons."""
import copy,hashlib,json,sqlite3,time
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _load_json

def load(vault,workflow_id,*,home=None):
    from ingest_kanban import IngestKanbanAdapter
    from fixed_semantic_recovery import native_proof,permitted_tasks,proof_scope,_request_text
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_schema import schema_for
    from fixed_page_evidence import restore_compact
    from revision_disposition import native_user
    from review_pair_request import canonical
    from paired_reviews import pseudo
    vault=Path(vault);home=Path(home) if home else Path.home()/'.hermes';root=vault/f'_system/ledgers/ingest-workflows/{workflow_id}'
    adapter=IngestKanbanAdapter(vault);w=adapter.workflow.status(workflow_id)
    card=next(c for c in w['kanban']['task_map'] if c['node']=='build-finalize')
    binding=_load_json(root/f'bindings/{fingerprint(card["idempotency_key"])}.json');permitted=permitted_tasks(adapter,binding,compact=True)
    snapshots=[row for p in (root/'semantic-inputs').glob('review-*.json') if (row:=_load_json(p)).get('contract')=='fixed-review-input/v1']
    rows=[];seen=set()
    with proof_scope(),sqlite3.connect('file:'+str(home/'state.db')+'?mode=ro',uri=True) as db:
        for path in (root/'semantic-calls').glob('*.json'):
            audit=_load_json(path)
            if audit.get('phase')!='page-review' or audit.get('reused_from_audit'):continue
            users=[u for (c,) in db.execute("SELECT content FROM messages WHERE session_id=? AND role='user'",(audit['model']['session_id'],)) if (u:=native_user(c,audit)) is not None]
            if len(users)!=1:raise ContractError('STALE_INPUT','$','saved review has no exact native user input')
            view=json.loads(_request_text(users[0]))['input'];view=restore_compact(view) if view.get('evidence_encoding') else view
            from fixed_semantic_media import image_catalog,select_images
            evidence=view['original_evidence'];images=select_images(evidence['tasks'],image_catalog(adapter,evidence))
            sc=schema_for('page-review',view);prepared=prepare_request_projection('page-review',view,sc,images)
            if prepared['wire']['user']!=users[0] or not native_proof(adapter,binding,path,prepared['wire'],sc,images,permitted,home=home):
                raise ContractError('STALE_INPUT','$','saved review input/output native proof differs')
            choices=[]
            for snapshot in snapshots:
                for source in snapshot['views']:
                    fields=('identity','path','content','existing_page','qa_status','business_status','visibility','citation_evidence','original_evidence')
                    if any(source.get(k)!=view.get(k) for k in fields):continue
                    meta=snapshot['backend']['pages'][source['page']]
                    choices.append((source,meta))
            if not choices:raise ContractError('STALE_INPUT','$','no original full review snapshot matches')
            source,meta=choices[0];page=meta['page'];pid=page['page_id']
            if pid in seen:raise ContractError('STALE_INPUT','$','multiple actual reviews for one page require explicit accounting')
            seen.add(pid);v=canonical(source,pid,meta['assessment'],page.get('revision_id'));p=pseudo(page,v)
            usage=db.execute('SELECT input_tokens,output_tokens,api_call_count FROM sessions WHERE id=?',(audit['model']['session_id'],)).fetchone()
            from fixed_semantic_recovery import transcript_reply
            from fixed_semantic import decode_semantic_response
            response=decode_semantic_response(transcript_reply(audit,prepared['wire'],home=home))[0]
            rows.append({'page':p,'view':v,'alias':source['page'],'assessment':meta['assessment'],'audit_ref':path.relative_to(vault).as_posix(),
                'native_request_sha256':audit['input_sha256'],'usage':dict(zip(('input','output','api_calls'),usage)),
                'response':response,'single_request':prepared['request_cost'],'single_input_size':prepared['input_size']})
    return sorted(rows,key=lambda r:int(r['alias'][1:])),adapter

def compare(rows,adapter,profile_name):
    from review_pair_request import prepare
    from fixed_semantic import prepare_request_projection
    from input_budget import profile
    budget=profile(profile_name);pending=list(rows);groups=[];held=[]
    while pending:
        first=pending.pop(0);chosen=[first];candidate=None
        if pending:
            trial=prepare([(r['page'],r['view']) for r in (first,pending[0])],prepare_request_projection,adapter=adapter,budget=budget)
            if trial[2]['budget_check']['fits']:chosen.append(pending.pop(0));candidate=trial
        candidate=candidate or prepare([(r['page'],r['view']) for r in chosen],prepare_request_projection,adapter=adapter,budget=budget)
        view,_,p,_,variants=candidate
        row={'pages':[r['page']['path'] for r in chosen],**p['budget_check'],
            'estimated_output_tokens':p['estimated_review_output_tokens'],
            'variants':[{'encoding':v.get('evidence_encoding','original'),**x['budget_check']} for v,x in variants]}
        (groups if p['budget_check']['fits'] else held).append(row)
    return {'profile':profile_name,'pages':len(rows),'estimated_requests':len(groups),'paired_requests':sum(len(g['pages'])==2 for g in groups),
        'held_pages':len(held),'request_groups':groups,'held':held,'eliminated_review_triggers':0,
        'requests_saved_by_batching_only':len(rows)-len(groups)-len(held),
        'actual_model_calls':0,'actual_token_usage':None,'model_wait_seconds':None,
        'scope':'same saved page contents, evidence and risk standards; mechanical complete request comparison only'}
