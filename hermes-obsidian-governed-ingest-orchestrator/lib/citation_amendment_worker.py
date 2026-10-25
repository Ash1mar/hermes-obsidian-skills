"""Native local updates, with exact preservation of all unrelated semantic work."""
import copy
import hashlib
from hermes_source_units.citation_amendment import state,plan,save,root,fail
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint
from citation_amendment_calls import job
from citation_local_proof import aliases,rebase_page,page_proof
import fixed_reduce

def save_result(adapter,value,name,data):
    amendment=state(adapter.workflow.vault,value['binding']['workflow_id'])
    path=root(adapter.workflow.vault,value['binding']['workflow_id'])/amendment['operation_id']/(name.removesuffix('.json')+'-'+fingerprint(data)+'.json')
    _write_atomic(path,_json_bytes(data));return path.relative_to(adapter.workflow.vault).as_posix()

def refs_key(ref):return ref['pass_id'],ref['candidate_id']

def successor_ref(ref,frozen,amendment):
    return {**ref,'pass_id':amendment['successor']['pass_id'] if ref['pass_id']==frozen['selection']['pass_id'] else ref['pass_id']}

def source_scope(frozen,amendment,value,old):
    selected={(frozen['selection']['pass_id'],cid) for cid in amendment['delta']['domain_affected_candidate_ids']}
    affected=set(selected)
    # Proposals are indivisible. Expand only along actual old membership and
    # new exact deduplication; no name/section proximity is treated as an edge.
    changed=True
    while changed:
        previous=set(affected)
        for p in old['proposals']:
            keys={refs_key(r) for r in p['candidate_refs']}
            if keys&affected:affected.update(keys)
        current={refs_key(successor_ref({'pass_id':p,'candidate_id':c},frozen,amendment)) for p,c in affected}
        for refs in value['backend']['mapping'].values():
            if {refs_key(r) for r in refs}&current:
                affected.update((frozen['selection']['pass_id'] if r['pass_id']==amendment['successor']['pass_id'] else r['pass_id'],r['candidate_id']) for r in refs)
        changed=affected!=previous
    handles=[h for h,refs in value['backend']['mapping'].items()
             if any(refs_key(successor_ref({'pass_id':p,'candidate_id':c},frozen,amendment)) in {refs_key(r) for r in refs} for p,c in affected)]
    return affected,handles

def source_parts(value,frozen,amendment,old):
    affected,handles=source_scope(frozen,amendment,value,old)
    mapping={refs_key(r):h for h,refs in value['backend']['mapping'].items() for r in refs}
    def rebased(refs):return list(dict.fromkeys(mapping[refs_key(successor_ref(r,frozen,amendment))] for r in refs))
    retained=[{k:(rebased(v) if k=='candidate_refs' else copy.deepcopy(v)) for k,v in p.items() if k!='proposal_id'}
              for p in old['proposals'] if not ({refs_key(r) for r in p['candidate_refs']}&affected)]
    omitted=rebased(old['omitted_candidate_refs'])
    if set(omitted)&set(handles):fail('selected correction was previously source-omitted; explicit scope revision is required')
    return affected,handles,retained,omitted

def source_execute(adapter,binding,caller,input_ref,value,frozen,amendment):
    from fixed_semantic import model_call,attach_receipt
    from fixed_reduce_packets import validate_packet
    old=next(r for r in frozen['sources'] if r['resource_id']==value['backend']['resource_id'])
    affected,handles,retained,omitted=source_parts(value,frozen,amendment,old)
    calls=[]
    if handles:
        local=fixed_reduce.resource_view(value,set(handles));packet={'candidate_refs':handles,'view':local}
        with job('source:'+frozen['selection']['task_id']+':'+old['resource_id']):
            draft,call=model_call(adapter,binding,'resource-reduce',local,caller,30000,attempt_limit=1)
        validate_packet(packet,draft)
        if draft['omitted_candidate_refs']:fail('local source revision cannot silently create new coverage losses')
        retained.extend(draft['proposals']);calls.append(call)
    whole={'proposals':retained,'omitted_candidate_refs':omitted,'reason':'Bounded native citation amendment; unchanged proposals retained under exact candidate/inspection identity proof.'}
    expanded=fixed_reduce.expand(value,whole)
    intent={'old_reduction_id':old['reduction_id'],'input_ref':input_ref,'input_sha256':fingerprint(value),
            'affected_old_candidate_refs':[{'pass_id':p,'candidate_id':c} for p,c in sorted(affected)],
            'local_handles':handles,'native_calls':calls,'draft':whole,'expanded_request':expanded}
    intent_ref=save_result(adapter,value,'source-'+old['resource_id']+'.json',intent)
    amendment.setdefault('source_intents',{})[old['resource_id']]=intent_ref
    save(adapter.workflow.vault,binding['workflow_id'],amendment)
    result=fixed_reduce.submit(adapter,binding,input_ref,whole)
    for call in calls:attach_receipt(adapter,call,result)
    return adapter.worker_complete(binding)

def global_plan(adapter,binding,caller,value,frozen,amendment):
    import fixed_global_packets as g
    from fixed_semantic import model_call
    mapping=aliases(frozen['old_source'],value,frozen['selection']['pass_id'],amendment['successor']['pass_id'])
    oldpages=frozen['scope']['manifest']['allowed_pages']+frozen['scope']['manifest']['gaps']
    rebased=[rebase_page(p,mapping) for p in oldpages]
    changed={h for h,refs in frozen['old_source']['backend']['mapping'].items()
             if any(r['pass_id']==frozen['selection']['pass_id'] and r['candidate_id'] in amendment['delta']['domain_affected_candidate_ids'] for r in refs)}
    current_changed={mapping[h] for h in changed}
    # Source proposal changes can alter grouping even when a particular claim
    # did not change. Use old+new exact proposal membership to close that scope.
    for source in frozen['sources']:
        if source['reduction_id'] not in frozen['retired_source_ids']:continue
        intent=_load_json(_vault_path(adapter.workflow.vault,amendment['source_intents'][source['resource_id']]))
        observed=_load_json(_vault_path(adapter.workflow.vault,intent['input_ref']))
        fresh=fixed_reduce._snapshot(adapter,observed['binding'],{'batch_id':frozen['batch_id']})
        if any(observed[k]!=fresh[k] for k in ('contract','view','backend')):fail('source local input authority changed')
        affected,handles,retained,omitted=source_parts(fresh,frozen,amendment,source)
        from citation_amendment_calls import authenticate
        native={c['audit_ref']:c for c in authenticate(adapter,binding['workflow_id'],'source')}
        if handles:
            if len(intent['native_calls'])!=1 or intent['native_calls'][0] not in native:fail('source replacement needs its authenticated native local response')
            call=native[intent['native_calls'][0]]
            from fixed_semantic_recovery import _request_text
            import json
            local=fixed_reduce.resource_view(fresh,set(handles))
            if json.loads(_request_text(call['wire']['user']))['input']!=local:fail('native source did not observe its current complete selected material')
            from fixed_reduce_packets import validate_packet
            validate_packet({'candidate_refs':handles},call['body'])
            if call['body']['omitted_candidate_refs']:fail('native local source omitted necessary members')
            retained.extend(call['body']['proposals'])
        elif intent['native_calls']:fail('identity-only rebasing cannot introduce a source call')
        reconstructed={'proposals':retained,'omitted_candidate_refs':omitted,
            'reason':'Bounded native citation amendment; unchanged proposals retained under exact candidate/inspection identity proof.'}
        if reconstructed!=intent['draft'] or fixed_reduce.expand(fresh,reconstructed)!=intent['expanded_request']:
            fail('source submission intent differs from native output and unchanged semantic evidence')
        new=next(r for r in value['backend']['reductions'] if r['resource_id']==source['resource_id'])
        expected=intent['expanded_request']
        if any(new[k]!=expected[k] for k in ('proposals','omitted_candidate_refs','tasks')):fail('formal source differs from its native local submission intent')
        affected_keys={refs_key(r) for r in intent['affected_old_candidate_refs']}
        current_changed.update(mapping[h] for h,refs in frozen['old_source']['backend']['mapping'].items()
                               if affected_keys.intersection(refs_key(r) for r in refs))
    previous=None
    while previous!=current_changed:
        previous=set(current_changed)
        for p in rebased:
            if current_changed.intersection(p['candidate_refs']):current_changed.update(p['candidate_refs'])
        for p in value['view']['proposals']:
            if current_changed.intersection(p['candidate_refs']):current_changed.update(p['candidate_refs'])
    fixed=[p for p in rebased if not current_changed.intersection(p['candidate_refs'])]
    affected=[p for p in rebased if current_changed.intersection(p['candidate_refs'])]
    base=g.initial();base['pages']=fixed;base['proposal_refs']=[p['proposal'] for p in value['view']['proposals']]
    calls=[]
    if current_changed:
        sliced={'view':{'phase':'global-detail','candidates':[c for c in value['view']['candidates'] if c['candidate'] in current_changed],
                        'proposals':[],'existing_pages':value['view'].get('existing_pages',[])}}
        local=fixed_reduce.global_detail_view(sliced)
        local['coordination']={'contract':g.CONTRACT,'previous_state_sha256':g.state_digest(base),'proposal_refs':[],
            'planned_pages':[{**p,'topics':[],'source_paths':[]} for p in affected],
            'rule':'Revise ONLY these assigned candidate members. Return each exactly once, no omissions. Other identities, paths and membership are frozen; do not append to them. Preserve conditions, exceptions and source restrictions.'}
        packet={'view':local,'candidate_refs':sorted(current_changed),'proposal_refs':[],
                'previous_state_sha256':g.state_digest(base),'units':[],'unit_count':1}
        with job('plan:'+frozen['selection']['task_id']):
            draft,call=model_call(adapter,binding,'global-detail',local,caller,30000,attempt_limit=1)
        if draft.get('omitted_candidate_refs'):fail('local planning cannot discard an assigned candidate')
        old_fixed={fingerprint(p['identity']):p for p in fixed}
        updated=g.accept(value,base,packet,draft)
        if any(fingerprint(p['identity']) in old_fixed and p!=old_fixed[fingerprint(p['identity'])] for p in updated['pages']):
            fail('local plan changed a frozen page')
        base=updated;calls.append(call)
    g.complete(value,base)
    return base,mapping,calls,{p['path'] for p in affected}

def global_execute(adapter,binding,caller,input_ref,value,frozen,amendment):
    from hermes_source_units.reduce_coverage import active
    from fixed_semantic import model_call
    import fixed_global_packets as g
    scope=active(adapter.workflow.vault,adapter.workflow.status(binding['workflow_id']))
    if amendment['phase']=='awaiting_acceptance':
        if not scope or scope['ref']['manifest_sha256']!=amendment['manifest_sha256']:fail('approve the newly generated exact limited manifest before formal submission')
        path=_vault_path(adapter.workflow.vault,amendment['work_ref'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=amendment['work_sha256']:fail('local native work certificate changed')
        work=_load_json(path)
        if fingerprint({k:value[k] for k in ('contract','view','backend')})!=work['source_authority_sha256']:fail('local downstream inputs changed')
        effective=copy.deepcopy(work['state']);written=scope['manifest']['allowed_pages']
        allowed_paths={p['path'] for p in written}
        effective['pages']=[p for p in effective['pages'] if p['path'] in allowed_paths]
        effective['omitted_candidate_refs']=[c for p in scope['manifest']['gaps'] for c in p['candidate_refs']]
        draft=g.complete(value,effective);contents={p['path']:_vault_path(adapter.workflow.vault,p['draft_ref']).read_text() for p in written}
        for p in draft['pages']:p['content']=contents[p['path']]
        result=fixed_reduce.submit(adapter,binding,input_ref,draft)
        return adapter.worker_complete(binding)
    planned,mapping,plan_calls,affected_paths=global_plan(adapter,binding,caller,value,frozen,amendment)
    old_allowed={p['path']:p for p in frozen['scope']['manifest']['allowed_pages']}
    old_gaps={p['path']:p for p in frozen['scope']['manifest']['gaps']}
    written=[];held=[];reuse=[];body_calls=[]
    for page in planned['pages']:
        raw_view=fixed_reduce.page_view(adapter,value,page)
        old=old_allowed.get(page['path'])
        if old:
            from hermes_source_units import ContractError
            try:cert=page_proof(frozen['old_source'],value,old,page,adapter)
            except ContractError:
                if page['path'] not in affected_paths:raise  # Never expand a failed proof into an automatic rewrite.
            else:
                raw=_vault_path(adapter.workflow.vault,old['draft_ref']).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=old['draft_sha256']:fail('old native body changed')
                written.append({**page,'content':raw.decode(),'draft_ref':old['draft_ref'],'audit_ref':old['audit_ref'],
                                'input_sha256':fingerprint(raw_view)})
                reuse.append({'path':page['path'],'origin':old,'proof':cert});continue
        if page['path'] not in affected_paths and page['path'] in old_gaps:
            held.append({**page,'reason':old_gaps[page['path']].get('reason','Prior necessary coverage remains deferred')});continue
        # A new/split page must be composed exclusively of the selected local
        # closure. All unchanged pages above were proven or remained deferred.
        view={**raw_view,'page_scope_contract':'assigned-candidates/v1'}
        logical='body:'+frozen['selection']['task_id']+':'+fingerprint(page['identity'])
        from fixed_page_work import LOCAL
        from hermes_source_units import ContractError
        try:
            with job(logical):draft,call=model_call(adapter,binding,'page-write',view,caller,30000,attempt_limit=1)
        except ContractError as exc:
            if exc.code not in LOCAL:raise
            held.append({**page,'reason':str(exc),'code':exc.code,'logical_work':logical,
                         'audit_ref':getattr(exc,'semantic_audit_ref',None),'completed':False})
            continue
        raw=draft['content'].encode();path=f'_system/ledgers/ingest-workflows/{binding["workflow_id"]}/page-drafts/{fingerprint(view)}.md'
        _write_atomic(_vault_path(adapter.workflow.vault,path),raw)
        written.append({**page,'content':draft['content'],'draft_ref':path,'audit_ref':call,'input_sha256':fingerprint(raw_view)})
        body_calls.append(call)
    work={'contract':'native-page-work/v1','binding':binding,'input_ref':input_ref,'state':planned,
          'source_authority_sha256':fingerprint({k:value[k] for k in ('contract','view','backend')}),
          'written':[{k:v for k,v in p.items() if k!='content'} for p in written],'held':held,'reuse_proofs':reuse,
          'successful_call_refs':plan_calls+body_calls,'planning_failure':None,'formal_global_submitted':False}
    ref=save_result(adapter,value,'page-work.json',work)
    from citation_amendment_admission import manifest
    manifest_ref,digest=manifest(adapter,value,work,frozen,amendment)
    amendment.update(phase='awaiting_acceptance',work_ref=ref,work_sha256=hashlib.sha256(_vault_path(adapter.workflow.vault,ref).read_bytes()).hexdigest(),
                     manifest_ref=manifest_ref,manifest_sha256=digest)
    save(adapter.workflow.vault,binding['workflow_id'],amendment)
    from hermes_source_units import ContractError
    raise ContractError('WORKFLOW_STOPPED','$','Local native revision finished; cancel/drain and explicitly approve new limited manifest: '+manifest_ref)

def execute(adapter,binding,caller,input_ref,value):
    amendment=state(adapter.workflow.vault,binding['workflow_id']);frozen=plan(adapter.workflow.vault,amendment)
    if binding['node'].startswith('resource-reduce:'):
        return source_execute(adapter,binding,caller,input_ref,value,frozen,amendment)
    return global_execute(adapter,binding,caller,input_ref,value,frozen,amendment)
