"""Scoped P4 audit releases with separate business/index eligibility and no raw expansion."""
from .source_units import _load_json,_vault_path,_sha
from .validation import fingerprint,validate_record
from .runtime_telemetry import span
from .delivery_scope import dependency_partition,fail,authorized
from .partial_finalize import pin,pinned
from .short_build import content_isolation, evidence as short_evidence, identity as short_identity, output_path, qa_advisory


def short_scope(service, runs):
    proofs = [r['short_evidence'] for r in runs if r.get('short_evidence')]
    selected = {p['page_id'] for r in runs if r.get('short_evidence') for p in r['page_revisions']}
    excluded = {}
    for run in runs:
        if not run.get('short_evidence'):
            continue
        for row in short_evidence(service.build, run)['scope']['excluded']:
            identity, page_id = short_identity(service.build, row['short_id'], row['name'])
            path = output_path(row['short_id'], row.get('card_path'), page_type=row.get('page_type'))
            excluded[page_id] = {**row['disposition'], 'identity': identity, 'path': path,
                                 'original_scope': run['short_evidence']}
    if selected.intersection(excluded):
        fail('short Build selected an excluded page')
    for run in runs:
        if not run.get('short_evidence'):
            continue
        pointer, rows = content_isolation(service.build, run)
        if pointer:
            proofs.append(pointer)
        for row in rows:
            decision = next(d for d in run['decisions'] if d['page_id'] == row['page_id'])
            excluded[row['page_id']] = {'category': 'unresolved', 'reason': row['reason'],
                                       'reopen_condition': 'verify a new revision of the affected page',
                                       'identity': decision['identity'], 'path': decision['path'],
                                       'original_scope': pointer}
    return selected, excluded, proofs


def retained_exclusions(service, exclusions, base_id, previous, current):
    """A selection boundary cannot retract an unchanged qualified base revision."""
    prior={(p['page_id'],p['current_revision_id']) for p in previous}
    unchanged={p['page_id'] for p in current if (p['page_id'],p['current_revision_id']) in prior}
    result=dict(exclusions)
    for pid,row in list(result.items()):
        if (pid in unchanged and row.get('category')=='delivery_conditions'
                and row.get('reason')=='outside-exact-short-build-selection'):
            scope=pinned(service.vault,row['original_scope'])['scope']
            if scope['authorization']['base_release']==base_id:
                del result[pid]
    return result

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
    short_pages,short_excluded,short_proofs=short_scope(service,runs)
    if prior:
        short_excluded=retained_exclusions(service,short_excluded,prior,old['subjects'],active)
    for run in runs:
        if run.get('short_evidence') and (advisory := qa_advisory(service.build,run)[0]):
            short_proofs.append(advisory)
    exclusions.update(short_excluded)
    if prior and short_pages:
        active_ids = {s['page_id'] for s in active}
        for row in old.get('delivery', {}).get('excluded', []):
            if row['page_id'] in active_ids - allowed:
                exclusions.setdefault(row['page_id'], row)
    source_eligibility={r['resource_id']:r for r in service._source_eligibility(current,change_map) if r['resource_id']}
    from .page_governance import reasons as governance_reasons, release_pointer
    governance=request.get('page_governance')
    candidates=[];rows={};eligible={};contents={}
    for subject in active:
        pid=subject['page_id']
        if pid not in allowed:
            exclusions.setdefault(pid,{'page_id':pid,'path':subject['current_path'],'category':'delivery_conditions',
                'reason':'outside-selected-delivery','reopen_condition':'authorize a new exact delivery'})
            candidates.append({'page_id':pid,'path':subject['current_path'],'identity':subject,'content':''})
            continue
        page=service._page_revision(pid,subject['current_revision_id'])
        path=_vault_path(service.vault,subject['current_path'])
        from .release_reads import body
        data=body(path) if path.is_file() else None
        if data is None or _sha(data)!=page['authored_sha256']:
            fail('canonical page changed before scoped publication','SOURCE_CHANGED')
        reasons=[];delivery_reasons=[]
        if pid not in allowed:delivery_reasons.append('outside-selected-delivery')
        prior_pointer = (release_pointer(service,old,page) if prior and
                         (pid,page['revision_id']) in published else None)
        reasons.extend(governance_reasons(service,page,governance or prior_pointer))
        if (governance or pid in short_pages) and reasons:delivery_reasons.extend(reasons)
        groups={'active':[],'stale':[],'withdrawn':[]}
        for ref in page['support_refs']:
            groups[service._contribution(ref,current,change_map)].append(ref)
            if not source_eligibility.get(ref['unit_ref']['resource_id'],{}).get('eligible'):
                reasons.append('source-governance-ineligible')
        if groups['stale'] or groups['withdrawn'] or not groups['active']:delivery_reasons.append('source-support-not-current')
        if 'source-governance-ineligible' in reasons:delivery_reasons.append('source-governance-ineligible')
        if delivery_reasons and pid not in exclusions:
            exclusions[pid]={'page_id':pid,'path':page['path'],'category':'delivery_conditions',
                'reason':','.join(sorted(set(delivery_reasons))),'reopen_condition':'verify this page delivery conditions and create a new exact release plan'}
        contents[pid]=data.decode('utf-8')
        candidates.append({'page_id':pid,'path':page['path'],'identity':subject,'content':contents[pid]})
        rows[pid]={'subject_id':subject['subject_id'],'page_id':pid,'current_revision_id':subject['current_revision_id'],
            'current_path':subject['current_path'],'active_support_refs':groups['active'],'stale_support_refs':[],
            'withdrawn_support_refs':[],'disposition':'current','reasons':[],'affected':True}
        eligible[pid]={'kind':'knowledge_page','id':pid,'eligible':not reasons,'reasons':sorted(set(reasons)),
            'resource_id':None,'unit_set_id':None,'page_revision_id':subject['current_revision_id'],
            'source_unit_refs':groups['active']}
    # Include excluded draft identities in link resolution, so links to them
    # exclude dependents instead of accidentally resolving to a different name.
    known={p['page_id'] for p in candidates}
    candidates.extend({'page_id':pid,'path':row['path'],'identity':row['identity'],'content':''}
                      for pid,row in short_excluded.items() if pid not in known)
    known.update(short_excluded)
    for row in scopes.values():
        snapshot=pinned(service.vault,row['scope']['snapshot'])
        for view in snapshot['views']:
            p=snapshot['backend']['pages'][view['page']]['page']
            if p['page_id'] in known:continue
            known.add(p['page_id']);candidates.append({'page_id':p['page_id'],'path':view['path'],
                'identity':view['identity'],'content':view['content']})
    partition=dependency_partition(candidates,exclusions)
    included=set(partition['included'])&set(rows)
    subjects=[s for s in active if s['page_id'] in included]
    with span('release_scope_summary',vault=service.vault, unique_body_reads=len(contents),
              identity_count=len(candidates),included=len(included),excluded=len(partition['excluded'])):
        navigation,blockers=service._navigation(subjects, contents=contents, scoped=True);redirects=service._redirects(subjects)
    if not included:blockers.append('no-deliverable-pages')
    if short_pages and not included.intersection(short_pages) and not scopes and all(r.get('short_evidence') for r in runs):
        blockers.append('no-qualified-short-pages')
    source_by_page={p['page_id']:row['closure']['authorization'] for row in scopes.values() for p in row['scope']['excluded']}
    source_by_page.update({p['page_id']:r['short_evidence'] for r in runs if r.get('short_evidence')
                           for p in r['page_revisions']})
    # Carry previously published short QA provenance into later scoped releases.
    previous_qa = [p for p in (old.get('delivery',{}).get('inherited_coverage_gaps',[]) if prior else [])
                   if p['ref'].endswith('/qa-advisory.json') and p not in short_proofs]
    delivery={'contract':'scoped-publication/v1','coverage_complete':False,
        'base_release':(pin(service.vault,service._manifest_path(prior).relative_to(service.vault).as_posix()) if prior else None),
        'included_page_ids':sorted(included),'excluded':[{'page_id':k,'path':v.get('path') or next(p['path'] for p in candidates if p['page_id']==k),
            'category':v['category'],'reason':v['reason'],'reopen_condition':v['reopen_condition'],
            'depends_on':v.get('depends_on',[]),'original_scope':v.get('original_scope') or source_by_page.get(k)}
            for k,v in sorted(partition['excluded'].items())],
        'inherited_coverage_gaps':list({r['closure']['authorization']['ref']:r['closure']['authorization'] for r in scopes.values()}.values())
            + [r['coverage_scope'] for r in runs if r.get('coverage_scope')] + short_proofs + previous_qa,
        'coverage_gap_summaries':[{'scope_id':r['scope']['scope_id'],'held_page_count':r['scope']['coverage_gaps'].get('held_page_count',0),
            'deferred_candidate_occurrences':r['scope']['coverage_gaps'].get('deferred_candidate_occurrences',0)} for r in scopes.values()],
        'raw_source_indexing':False,'dependencies':[{'page_id':k,'depends_on':v} for k,v in partition['dependencies'].items()]}
    plan={'contract':'hermes-vault-finalize-plan/v1','release_id':str(request['release_id']),
        'revision':1,'state':'draft','actor':str(request['actor']),'expected_state_revision':state['revision'],
        'identity_registry_revision':registry['revision'],'build_runs':[{'run_id':r['run_id'],'revision':r['revision']} for r in runs],
        'source_changes':changes,'subjects':[rows[p] for p in sorted(included)],'redirects':redirects,
        'navigation':navigation,'index_eligibility':[eligible[p] for p in sorted(included)],
        'blockers':sorted(set(blockers)),'reason':str(request.get('reason','')),
        'delivery':delivery}
    if request.get('delivery_scopes'):plan['delivery_scopes']=request['delivery_scopes']
    if governance:plan['page_governance']=governance
    plan['plan_id']=fingerprint({k:v for k,v in plan.items() if k not in ('revision','state')})
    validate_record('vault_finalize_plan',plan);return plan

def validate_manifest(service,plan,release,*,scopes=None):
    for key in ('subjects','index_eligibility','build_runs','source_changes','redirects'):
        if release[key]!=plan[key]:
            fail('published facts differ from the applied plan','SOURCE_CHANGED')
    if (release.get('page_governance')!=plan.get('page_governance') or release.get('delivery_scopes')!=plan.get('delivery_scopes') or release.get('delivery')!=plan.get('delivery')):
        fail('published scope or exclusions differ from the applied plan','SOURCE_CHANGED')
    selected=set(release['delivery']['included_page_ids']);excluded={p['page_id'] for p in release['delivery']['excluded']}
    if fingerprint({k:v for k,v in plan.items() if k not in ('revision','state','plan_id')})!=plan['plan_id']:
        fail('scoped plan content differs from its identity','SOURCE_CHANGED')
    from .delivery_scope import release_closures
    if scopes is None:scopes=release_closures(service.build,release.get('delivery_scopes',[]),[r['run_id'] for r in release['build_runs']],historical=True)
    allowed={p['page']['page_id'] for row in scopes.values() for p in row['closure']['pages']}
    completed=[]
    for ref in release['build_runs']:
        if ref['run_id'] in scopes:continue
        run=_load_json(service.build._run_path(ref['run_id']))
        if run['state']!='completed' or run['revision']!=ref['revision']:
            fail('complete delivery Build changed','SOURCE_CHANGED')
        service.build.validate_run(ref['run_id'])
        completed.append(run)
        allowed.update(p['page_id'] for p in run['page_revisions'])
    required_exclusions={p['page_id'] for row in scopes.values() for p in row['scope']['excluded']}
    short_pages,short_excluded,short_proofs=short_scope(service,completed)
    if any(p not in release['delivery']['inherited_coverage_gaps'] for p in short_proofs):
        fail('release omitted short Build coverage evidence','SOURCE_CHANGED')
    if release['delivery'].get('base_release'):
        previous=pinned(service.vault,release['delivery']['base_release'])
        short_excluded=retained_exclusions(service,short_excluded,previous['release_id'],
                                           previous['subjects'],release['subjects'])
        prior={(p['page_id'],p['current_revision_id']) for p in previous['subjects']}
        allowed.update(p['page_id'] for p in release['subjects'] if (p['page_id'],p['current_revision_id']) in prior)
    required_exclusions.update(short_excluded)
    if not selected.issubset(allowed) or not required_exclusions.issubset(excluded):
        fail('release omitted exclusions or exceeded its delivery','SOURCE_CHANGED')
    if (selected&excluded or selected!={s['page_id'] for s in release['subjects']}
            or selected!={r['id'] for r in release['index_eligibility']}
            or any(r['kind']!='knowledge_page' for r in release['index_eligibility'])):
        fail('release subjects/index escaped the exact eligible page set','SOURCE_CHANGED')
    for item in release['index_eligibility']:
        page=service._page_revision(item['id'],item['page_revision_id'])
        acceptance=(page.get('review') or {}).get('acceptance') or {}
        if item['eligible'] and acceptance.get('short_evidence') and page['qa_status']!='usable':
            ref=acceptance['short_evidence']['ref'].replace('/short-evidence.json','/qa-advisory.json')
            pins=[p for p in release['delivery']['inherited_coverage_gaps'] if p['ref']==ref]
            if len(pins)!=1:
                fail('published short page lost its exact QA advisory','SOURCE_CHANGED')
            pinned(service.vault,pins[0])
        from .page_governance import reasons, release_pointer
        if (item['eligible'] or item['id'] in short_pages) and reasons(service,page,release_pointer(service,release,page)):
            fail('published page lacked its own required approval','SOURCE_CHANGED')
