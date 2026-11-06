"""Read-only cost/duplication inventory; no hypothetical semantic outputs."""
import copy,json,sqlite3,time
from pathlib import Path
from collections import Counter
from hermes_source_units.validation import fingerprint
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json

def repeats(values):
    sizes={};counts=Counter()
    for v in values:
        raw=json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode();k=fingerprint(v);sizes[k]=len(raw);counts[k]+=1
    total=sum(sizes[k]*n for k,n in counts.items());duplicate=sum(sizes[k]*(n-1) for k,n in counts.items())
    return {'serialized_value_bytes':total,'exact_duplicate_value_bytes':duplicate,'duplicate_fraction':duplicate/total if total else 0,
        'note':'Representation opportunity only; source ownership/QA still required. Not measured billable token savings.'}

def audit(vault,wid):
    from ingest_kanban import IngestKanbanAdapter
    from revision_disposition import native_user
    from fixed_semantic_recovery import _request_text,native_proof,permitted_tasks,proof_scope
    from fixed_semantic import system_prompt,prepare_request_projection
    from fixed_semantic_media import image_catalog,select_images
    from page_planning_budget import operation,hints
    from input_budget import profile
    adapter=IngestKanbanAdapter(vault);root=Path(vault)/f'_system/ledgers/ingest-workflows/{wid}'
    w=adapter.workflow.status(wid);by_node={c['node']:c for c in w['kanban']['task_map']};phases={};details=[];sources=[];views=[]
    with proof_scope(),sqlite3.connect('file:'+str(Path.home()/'.hermes/state.db')+'?mode=ro',uri=True) as db:
        for p in (root/'semantic-calls').glob('*.json'):
            a=_load_json(p)
            if a.get('reused_from_audit'):continue
            session=a.get('model',{}).get('session_id');usage=db.execute('SELECT input_tokens,output_tokens,api_call_count FROM sessions WHERE id=?',(session,)).fetchone() if session else None
            phase=a['phase'];r=phases.setdefault(phase,{'native_audits':0,'reported_input_tokens':0,'reported_output_tokens':0,'usage_unknown_audits':0,'summed_model_elapsed_ms':0})
            r['native_audits']+=1;r['summed_model_elapsed_ms']+=a.get('model_elapsed_ms',0)
            if usage is None or usage[0] is None:r['usage_unknown_audits']+=1
            if usage:r['reported_input_tokens']+=usage[0] or 0;r['reported_output_tokens']+=usage[1] or 0
            if phase not in ('resource-reduce','global-plan','global-detail'):continue
            users=[u for (c,) in db.execute("SELECT content FROM messages WHERE session_id=? AND role='user'",(session,)) if (u:=native_user(c,a)) is not None]
            if len(users)!=1:raise ContractError('STALE_INPUT','$','planning audit lacks unique native input')
            body=json.loads(_request_text(users[0]));v=body['input'];sc=body['output_schema'];wire={'system':system_prompt(phase,v,body.get('correction')),'user':users[0]}
            card=by_node[a['node']];binding=_load_json(root/f'bindings/{fingerprint(card["idempotency_key"])}.json')
            e=v.get('original_evidence',{});images=select_images(e.get('tasks',[]),image_catalog(adapter,e))
            if not native_proof(adapter,binding,p,wire,sc,images,permitted_tasks(adapter,binding,compact=True)):
                raise ContractError('STALE_INPUT','$','planning input lacks native proof')
            if phase=='global-detail':details.append({'audit_ref':p.relative_to(vault).as_posix(),'reason':'page_input_budget' if 'page_budget' in v else 'semantic_detail',
                'original_page':v.get('page_budget',{}).get('original_page'),'suggested_fit_groups':v.get('page_budget',{}).get('suggested_fit_candidate_groups'),
                'original_input_codepoints':a['input_codepoints'],'reported_input_tokens':usage[0] if usage else None,
                'avoidable_before_model':'budget pressure mechanically detectable; avoided calls and new page count require native semantic validation' if 'page_budget' in v else 'semantic correction retained'})
            if phase=='resource-reduce':
                from efficiency_presentation import qa_restore
                raw=qa_restore(v);sources.append({'audit_ref':p.relative_to(vault).as_posix(),
                    'qa_repetition':repeats([c['evidence_qa'] for c in raw.get('candidates',[]) if 'evidence_qa' in c]),
                    'original_text_repetition':repeats(list(e.get('texts',{}).values())),
                    'packet_expansion_enabled':False})
    candidates=[]
    for p in (root/'semantic-inputs').glob('*.json'):
        v=_load_json(p)
        if v.get('contract')=='fixed-reduce-input/v1' and v.get('backend',{}).get('kind')=='global-reduce':
            if fingerprint({k:x for k,x in v.items() if k!='input_id'})!=v['input_id']:raise ContractError('STALE_INPUT','$','saved global snapshot changed')
            candidates.append((p,v))
    if candidates:
        _,v=max(candidates,key=lambda row:len(row[1]['view']['candidates']));v=copy.deepcopy(v)
        universe=[c['candidate'] for c in v['view']['candidates']];profiles=[]
        for name in ('baseline-30000','pages-40000'):
            v['view'].update(body_call_contract='independent-page-body/v1',page_execution_contract='unified-page-execution/v3',early_page_budget_contract='proposal-final-body-cost/v1')
            v['backend']['input_budget']=profile(name)
            with operation(v):h=hints(v,universe,prepare_request_projection)
            profiles.append({'profile':name,'candidate_count':len(universe),'candidate_membership_sha256':fingerprint(universe),'hints':h})
    else:profiles=[]
    return {'contract':'planning-total-cost-audit/v1','phases':phases,'detail_reasons':dict(Counter(d['reason'] for d in details)),
        'detail_calls':details,'source_duplication':sources,'same_universe_early_hints':profiles,
        'cost_scope':'Resource Reduce + Global Plan + Detail + bodies + enabled reviews; native audits may omit transport failures. Unknown usage never counted as free.',
        'future_quality_or_coverage_change':None,'future_actual_tokens':None,'actual_new_model_calls':0,
        'model_wait_scope':'summed per-call milliseconds, overlaps possible; not workflow wall time',
        'maintenance_window':'operator report separately; no new native execution'}
