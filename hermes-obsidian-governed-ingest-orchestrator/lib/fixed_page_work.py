"""Bounded page planning and independent native drafts, using existing calls/journals.

No new recovery scope: cancelled execution replays exact native results normally.
Only Hermes chooses semantic identities, regrouping and prose.
"""
import copy
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _vault_path,_write_atomic,_json_bytes
from fixed_semantic_schema import schema
import fixed_reduce
import fixed_global_packets as g

LIMIT=30000
LOCAL={'READING_WINDOW_OVERSIZE','SEMANTIC_IMAGE_OVERSIZE','SEMANTIC_REVIEW_REQUIRED',
       'INVALID_SCHEMA','INCOMPLETE_COVERAGE','DUPLICATE','IDENTITY_CONFLICT',
       'UNRESOLVED_REFERENCE','SEMANTIC_OUTPUT_HELD'}


def measure(adapter,value,page,caller,project):
    from fixed_semantic import HermesCaller
    from fixed_semantic_media import image_catalog,select_images,require_images_fit
    view=fixed_reduce.page_view(adapter,value,page)
    images=select_images(view.get('original_evidence',{}).get('tasks',[]),
        image_catalog(adapter,view.get('original_evidence',{}))) if isinstance(caller,HermesCaller) else []
    try:require_images_fit(images)
    except ContractError as exc:
        if exc.code!='SEMANTIC_IMAGE_OVERSIZE':raise
        return view,LIMIT+1
    return view,project('page-write',view,schema('page-write'),images)['input_size']


def refinement(adapter,value,state,page,caller,project):
    handles=set(page['candidate_refs'])
    # Preserve all semantic fields needed for grouping; originals are never
    # substituted: each final body still reads its complete original packages.
    selected=[c for c in value['view']['candidates'] if c['candidate'] in handles]
    sliced={'view':{'phase':'global-detail','candidates':selected,'proposals':[],
        'existing_pages':[p for p in value['view'].get('existing_pages',[])
            if p['identity']['kind']==page['identity']['kind'] and p['identity']['identity_key']==page['identity']['identity_key']]}}
    if 'coverage_limitations' in value['view']:sliced['view']['coverage_limitations']=value['view']['coverage_limitations']
    view=fixed_reduce.global_detail_view(sliced)
    base=copy.deepcopy(state);base['pages']=[p for p in base['pages'] if p is not page and p!=page]
    # Fit groups are cost examples, never program-authored semantic identities.
    fits=[];members=[]
    for handle in page['candidate_refs']:
        trial={**page,'candidate_refs':[*members,handle]}
        _,size=measure(adapter,value,trial,caller,project)
        if size>LIMIT:
            if not members:raise ContractError('READING_WINDOW_OVERSIZE','$','one complete candidate/parent request cannot fit; retain it as held')
            fits.append(members);members=[handle]
            _,single=measure(adapter,value,{**page,'candidate_refs':members},caller,project)
            if single>LIMIT:raise ContractError('READING_WINDOW_OVERSIZE','$','one complete candidate/parent request cannot fit; retain it as held')
        else:members=trial['candidate_refs']
    if members:fits.append(members)
    view['coordination']={'contract':g.CONTRACT,'previous_state_sha256':g.state_digest(base),'proposal_refs':[],
        'planned_pages':[{'identity':copy.deepcopy(page['identity']),'path':page['path'],'topics':[],'source_paths':[]}],
        'rule':'Refine only this provisional oversized page. Return every supplied candidate exactly once, with no omissions. '
               'Choose the fewest meaningful page identities whose complete evidence fits the supplied budget. '
               'Preserve conditions, scope/version differences and all QA. Keep the original identity/path for one suitable subset when meaningful; '
               'other page identities and paths must be distinct. Fit groups are measured cost examples, not semantic decisions; '
               'you may choose different subsets if they fit. Do not solve budget by omitting evidence or candidates.'}
    view['page_budget']={'limit_codepoints':LIMIT,'original_page':{'identity':page['identity'],'path':page['path']},
        'suggested_fit_candidate_groups':fits,'check':'Program measures the complete page-write request for each output page; an oversized member set violates this output contract.'}
    packet={'view':view,'previous_state_sha256':g.state_digest(base),'candidate_refs':page['candidate_refs'],
        'proposal_refs':[],'units':[],'unit_count':1}
    return base,packet


def execute(adapter,binding,value,state,caller,model_call,attach_receipt,project,*,planning_failure=None):
    from semantic_output import execute as checked_output,check,stop_at_output_boundary
    from fixed_global_targets import select
    original_plan_sha=fingerprint(state);working=copy.deepcopy(state);written=[];calls=[];held=[]
    prefix=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}"
    progress_ref=f"{prefix}/semantic-inputs/page-work-{value['input_id']}.json"
    def save():
        _write_atomic(_vault_path(adapter.workflow.vault,progress_ref),_json_bytes({
            'contract':'native-page-work/v1','binding':binding,'original_plan_sha256':original_plan_sha,
            'current_plan_sha256':fingerprint(working),'written':[{k:v for k,v in p.items() if k!='content'} for p in written],'held':held,
            'planning_failure':planning_failure,'successful_call_refs':calls,'formal_global_submitted':False}))
    def defer(page,phase,exc):
        if exc.code not in LOCAL:raise exc
        held.append({'path':page['path'],'candidate_refs':page['candidate_refs'],'phase':phase,
            'code':exc.code,'reason':str(exc),'audit_ref':getattr(exc,'semantic_audit_ref',None)})
        save()
    def write(page,view):
        answer,audit,_=checked_output(adapter,binding,'page-write',view,caller,model_call,
            lambda d:check('page-write',value,None,None,d))
        content=answer['content'];ref=f"{prefix}/page-drafts/{fingerprint(view)}.md"
        adapter.worker_check(binding)
        path=_vault_path(adapter.workflow.vault,ref);raw=content.encode('utf-8')
        if path.exists() and path.read_bytes()!=raw:raise ContractError('SOURCE_CHANGED','$','saved native body differs for identical page input')
        if not path.exists():_write_atomic(path,raw)
        written.append({'identity':page['identity'],'path':page['path'],'candidate_refs':page['candidate_refs'],
            'content':content,'draft_ref':ref,'audit_ref':audit,'input_sha256':fingerprint(view)})
        calls.append(audit);attach_receipt(adapter,audit,{'ok':True,'validated':True,'state':'page_body_only','draft_ref':ref})
        save();stop_at_output_boundary(adapter,binding,'page-write',view)
    for original in list(state['pages']):
        page=next(p for p in working['pages'] if p['path']==original['path'])
        try:
            view,size=measure(adapter,value,page,caller,project)
            if size>LIMIT:
                base,packet=refinement(adapter,value,working,page,caller,project)
                packet=select(adapter,binding,'global-detail',packet,project)
                def validate(draft):
                    from fixed_global_targets import expand
                    updated=g.accept(value,base,packet,draft)
                    # The caller sees the exact dynamic member-set constraint.
                    expanded=expand('global-detail',packet['view'],draft)
                    for index,p in enumerate(expanded['pages']):
                        _,n=measure(adapter,value,p,caller,project)
                        if n>LIMIT:raise ContractError('INVALID_SCHEMA',f'$.pages[{index}].candidate_refs',
                            f'complete output page request has {n} codepoints, exceeds supplied {LIMIT}; choose a smaller complete member set without dropping claims')
                    return updated
                routed,audit,updated=checked_output(adapter,binding,'global-detail',packet['view'],caller,model_call,
                    validate,packet=packet,state=base)
                from fixed_global_targets import expand
                children=expand('global-detail',packet['view'],routed)['pages']
                working=updated;calls.append(audit)
                attach_receipt(adapter,audit,{'ok':True,'validated':True,'state':'budget_refined_page_plan','original_plan_sha256':original_plan_sha})
                save()
            else:children=[page]
        except ContractError as exc:
            defer(page,'page-plan',exc);continue
        for child in children:
            try:
                child_view,n=measure(adapter,value,child,caller,project)
                if n>LIMIT:raise ContractError('READING_WINDOW_OVERSIZE','$','complete page input exceeds bound')
                write(child,child_view)
            except ContractError as exc:defer(child,'page-write',exc)
    save()
    if held or planning_failure:
        raise ContractError('SEMANTIC_REVIEW_REQUIRED','$',
            f'Independent page work preserved: {len(written)} native bodies, {len(held)} held pages; inspect {progress_ref}')
    bodies={p['path']:p['content'] for p in written}
    draft=g.complete(value,working)
    for p in draft['pages']:p['content']=bodies[p['path']]
    return draft,calls
