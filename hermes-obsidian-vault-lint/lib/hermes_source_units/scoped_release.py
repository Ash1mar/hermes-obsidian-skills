"""Publish exactly eligible delivery pages; retain exclusions without raw-source expansion."""
from .source_units import _load_json,_vault_path,_sha
from .validation import fingerprint,validate_record
from .delivery_scope import dependency_partition,fail,authorized
from .partial_finalize import pin,pinned

def build_plan(service,request,state,runs,scopes):
    current=service._current_sets();changes=service._source_changes(request.get('source_changes',[]),current)
    change_map={r['resource_id']:r for r in changes};registry=service._registry()
    active=[s for s in registry['subjects'] if s['status']=='active']
    allowed={p['page_id'] for r in runs if r['run_id'] not in scopes for p in r['page_revisions']}
    allowed.update(p['page']['page_id'] for row in scopes.values() for p in row['closure']['pages'])
    # Retain only unchanged subjects from the preceding published release.
    prior=state.get('current_release_id')
    if prior:
        old=_load_json(service._manifest_path(prior))
        published={(p['page_id'],p['current_revision_id']) for p in old['subjects']}
        allowed.update(s['page_id'] for s in active if (s['page_id'],s['current_revision_id']) in published)
    exclusions={p['page_id']:{**p,'reason':p.get('reason','excluded by delivery scope')} for row in scopes.values() for p in row['scope']['excluded']}
    source_eligibility={r['resource_id']:r for r in service._source_eligibility(current,change_map) if r['resource_id']}
    candidates=[];rows={};eligible={}
    for subject in active:
        pid=subject['page_id'];page=service._page_revision(pid,subject['current_revision_id'])
        path=_vault_path(service.vault,subject['current_path'])
        if not path.is_file() or _sha(path.read_bytes())!=page['authored_sha256']:
            fail('canonical page changed before scoped publication','SOURCE_CHANGED')
        reasons=[]
        if pid not in allowed:reasons.append('outside-selected-delivery')
        for key,needed,reason in (('qa_status','usable','qa-required'),('business_status','approved','business-unassessed'),('visibility','published','visibility-draft')):
            if page[key]!=needed:reasons.append(reason)
        groups={'active':[],'stale':[],'withdrawn':[]}
        for ref in page['support_refs']:
            groups[service._contribution(ref,current,change_map)].append(ref)
            if not source_eligibility.get(ref['unit_ref']['resource_id'],{}).get('eligible'):
                reasons.append('source-governance-ineligible')
        if groups['stale'] or groups['withdrawn'] or not groups['active']:reasons.append('source-support-not-current')
        if reasons and pid not in exclusions:
            exclusions[pid]={'page_id':pid,'path':page['path'],'category':'publication_conditions',
                'reason':','.join(sorted(set(reasons))),'reopen_condition':'verify this page publication conditions and create a new exact release plan'}
        candidates.append({'page_id':pid,'path':page['path'],'identity':subject,'content':path.read_text(encoding='utf-8')})
        rows[pid]={'subject_id':subject['subject_id'],'page_id':pid,'current_revision_id':subject['current_revision_id'],
            'current_path':subject['current_path'],'active_support_refs':groups['active'],'stale_support_refs':[],
            'withdrawn_support_refs':[],'disposition':'current','reasons':[],'affected':True}
        eligible[pid]={'kind':'knowledge_page','id':pid,'eligible':True,'reasons':[],
            'resource_id':None,'unit_set_id':None,'page_revision_id':subject['current_revision_id'],
            'source_unit_refs':groups['active']}
    # Include excluded draft identities in link resolution, so links to them
    # exclude dependents instead of accidentally resolving to a different name.
    known={p['page_id'] for p in candidates}
    for row in scopes.values():
        from .partial_finalize import pinned
        snapshot=pinned(service.vault,row['scope']['snapshot'])
        for view in snapshot['views']:
            p=snapshot['backend']['pages'][view['page']]['page']
            if p['page_id'] in known:continue
            known.add(p['page_id']);candidates.append({'page_id':p['page_id'],'path':view['path'],
                'identity':view['identity'],'content':view['content']})
    partition=dependency_partition(candidates,exclusions)
    included=set(partition['included'])&set(rows)
    subjects=[s for s in active if s['page_id'] in included]
    navigation,blockers=service._navigation(subjects);redirects=service._redirects(subjects)
    if not included:blockers.append('no-publishable-pages')
    source_by_page={p['page_id']:row['closure']['authorization'] for row in scopes.values() for p in row['scope']['excluded']}
    delivery={'contract':'scoped-publication/v1','coverage_complete':False,
        'base_release':(pin(service.vault,service._manifest_path(prior).relative_to(service.vault).as_posix()) if prior else None),
        'included_page_ids':sorted(included),'excluded':[{'page_id':k,'path':v.get('path',next(p['path'] for p in candidates if p['page_id']==k)),
            'category':v['category'],'reason':v['reason'],'reopen_condition':v['reopen_condition'],
            'depends_on':v.get('depends_on',[]),'original_scope':source_by_page.get(k)} for k,v in sorted(partition['excluded'].items())],
        'inherited_coverage_gaps':list({r['closure']['authorization']['ref']:r['closure']['authorization'] for r in scopes.values()}.values()),
        'coverage_gap_summaries':[{'scope_id':r['scope']['scope_id'],'held_page_count':r['scope']['coverage_gaps'].get('held_page_count',0),
            'deferred_candidate_occurrences':r['scope']['coverage_gaps'].get('deferred_candidate_occurrences',0)} for r in scopes.values()],
        'raw_source_indexing':False,'dependencies':[{'page_id':k,'depends_on':v} for k,v in partition['dependencies'].items()]}
    plan={'contract':'hermes-vault-finalize-plan/v1','release_id':str(request['release_id']),
        'revision':1,'state':'draft','actor':str(request['actor']),'expected_state_revision':state['revision'],
        'identity_registry_revision':registry['revision'],'build_runs':[{'run_id':r['run_id'],'revision':r['revision']} for r in runs],
        'source_changes':changes,'subjects':[rows[p] for p in sorted(included)],'redirects':redirects,
        'navigation':navigation,'index_eligibility':[eligible[p] for p in sorted(included)],
        'blockers':sorted(set(blockers)),'reason':str(request.get('reason','')),
        'delivery_scopes':request['delivery_scopes'],'delivery':delivery}
    plan['plan_id']=fingerprint({k:v for k,v in plan.items() if k not in ('revision','state')})
    validate_record('vault_finalize_plan',plan);return plan

def validate_manifest(service,plan,release):
    if release.get('delivery_scopes')!=plan.get('delivery_scopes') or release.get('delivery')!=plan.get('delivery'):
        fail('published scope or exclusions differ from the applied plan','SOURCE_CHANGED')
    selected=set(release['delivery']['included_page_ids']);excluded={p['page_id'] for p in release['delivery']['excluded']}
    if fingerprint({k:v for k,v in plan.items() if k not in ('revision','state','plan_id')})!=plan['plan_id']:
        fail('scoped plan content differs from its identity','SOURCE_CHANGED')
    from .delivery_scope import release_closures
    scopes=release_closures(service.build,release['delivery_scopes'],[r['run_id'] for r in release['build_runs']],historical=True)
    allowed={p['page']['page_id'] for row in scopes.values() for p in row['closure']['pages']}
    required_exclusions={p['page_id'] for row in scopes.values() for p in row['scope']['excluded']}
    if release['delivery'].get('base_release'):
        from .partial_finalize import pinned
        previous=pinned(service.vault,release['delivery']['base_release'])
        prior={(p['page_id'],p['current_revision_id']) for p in previous['subjects']}
        allowed.update(p['page_id'] for p in release['subjects'] if (p['page_id'],p['current_revision_id']) in prior)
    if not selected.issubset(allowed) or not required_exclusions.issubset(excluded):
        fail('release omitted exclusions or exceeded its delivery','SOURCE_CHANGED')
    if (selected&excluded or selected!={s['page_id'] for s in release['subjects']}
            or selected!={r['id'] for r in release['index_eligibility']}
            or any(not r['eligible'] or r['kind']!='knowledge_page' for r in release['index_eligibility'])):
        fail('release subjects/index escaped the exact eligible page set','SOURCE_CHANGED')
    for item in release['index_eligibility']:
        page=service._page_revision(item['id'],item['page_revision_id'])
        if (page['qa_status'],page['business_status'],page['visibility'])!=('usable','approved','published'):
            fail('published page lacked its own required approval','SOURCE_CHANGED')
