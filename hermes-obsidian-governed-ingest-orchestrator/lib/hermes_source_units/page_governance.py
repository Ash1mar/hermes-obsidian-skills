"""Append-only qualification of accepted content for internal Vault retrieval.

This policy approves source-grounded retrieval, not professional applicability
or public access. QA is never cleared here. Missing/conflicting evidence holds
the affected page. Existing content, reviews, Passes and releases stay intact.
"""
from .source_units import _load_json, _vault_path, _json_bytes, _write_atomic, _sha
from .validation import ContractError, fingerprint, validate_record
from .partial_finalize import pin, pinned
from .runtime_telemetry import span

ROOT = '_system/page-governance'
POLICY = {
    'contract': 'internal-vault-retrieval-policy/v1',
    'purpose': 'source-grounded internal retrieval; no professional applicability approval',
    'qa': 'usable-only; unresolved QA and dependent pages remain isolated',
    'acceptance': 'reuse exact committed Build acceptance and any required native review',
    'sources': 'current, processed, active, approved source organizations',
    'visibility': 'published within the named Vault access boundary; never public access',
    'semantic_calls': 0,
}


def fail(message):
    raise ContractError('PAGE_GOVERNANCE_INVALID', '$', message)


def authorization(vault, pointer):
    value = pinned(vault, pointer)
    q = value['request']
    if (q.get('policy') != POLICY or q.get('automatic_page_approval') is not True
            or q.get('stop_after') != 'release_apply'
            or q.get('input_digest') != 'sha256:'+fingerprint({k:v for k,v in q.items() if k!='input_digest'})):
        fail('exact internal retrieval authorization required')
    return value


def historical_plan_ids(service, workflow):
    """Old repairs remain bound to their original published generation."""
    result = {workflow.get('release_plan_id')}
    for refresh in workflow.get('release_refresh_history', []):
        report = authorization(service.vault,refresh['authorization'])
        q = report['request']
        if (q['workflow_id']!=workflow['workflow_id'] or q['actor']!=workflow['actor']
                or q['operation_id']!=refresh['operation_id'] or q['input_digest']!=refresh['input_digest']
                or q['retrieval_scope']!=service.knowledge.source.vault_manifest['vault']['id']
                or report['previous_release']!=q['base_release']
                or report['previous_checkpoint']['state']!='approved'):
            fail('release refresh authority changed')
        base = pinned(service.vault,q['base_release'])
        validate_record('knowledge_release',base)
        result.add(base['plan_id'])
    return result


def qa_issues(service, page, selected):
    """Keep exact source checks; do not infer a QA fix from their wording."""
    issues = []
    for record, _ in selected:
        for check in record.get('inspections', []):
            if check.get('qa') != 'needs-qa':
                continue
            # Preserve all checks from the contributing Pass conservatively.
            import json
            issue = {'pass_id': record['pass_id'], 'check_json': json.dumps(check,ensure_ascii=False,sort_keys=True)}
            issue['issue_id'] = fingerprint(issue)
            if issue not in issues:
                issues.append(issue)
    return issues


def create(service, authorization_pointer):
    """Native planning worker performs deterministic governance, zero model work."""
    grant = authorization(service.vault, authorization_pointer)
    q = grant['request']
    from .workflow_guard import _ACTIVE, _BINDING
    active, binding = _ACTIVE.get(), _BINDING.get()
    if (not active or not binding or active['kind']!='vault-finalize-plan'
            or binding['node']!='vault-finalize-plan' or active['workflow_id']!=q['workflow_id']
            or active['actor']!=q['actor'] or active['vault']!=str(service.vault)):
        fail('governance writes require the bound native planning worker')
    from .initial_release import CONTRACT, admitted_pointer, validate as validate_initial
    initial = grant.get('contract') == CONTRACT
    if initial:
        from .ingest_workflow import FileIngestWorkflowService
        workflow = FileIngestWorkflowService(service.vault).status(q['workflow_id'])
        if admitted_pointer(service.vault, workflow) != authorization_pointer:
            fail('initial page qualification requires admitted recovery authority')
        base = validate_initial(service, workflow, authorization_pointer)
    else:
        base = pinned(service.vault, q['base_release'])
        service.validate(base['release_id'])
    if q['retrieval_scope'] != service.source.vault_manifest['vault']['id']:
        fail('retrieval authorization belongs to another Vault')
    exclusion_evidence=[]
    if initial and workflow.get('page_execution_contract')=='unified-page-execution/v3':
        from .qa_policy import reports
        exclusion_evidence=[pin(service.vault,ref) for ref in reports(service.vault,workflow['workflow_id'])]
        for pointer in workflow.get('execution_recovery_history',[]):
            proof=pin(service.vault,pointer['report_ref'])
            if proof['sha256']!=pointer['report_sha256']:fail('workflow exclusion authority changed')
            exclusion_evidence.append(proof)
    from .delivery_scope import release_closures
    scopes = release_closures(service.build, base.get('delivery_scopes', []),
                               [r['run_id'] for r in base['build_runs']])
    current = service._current_sets()
    source = {r['resource_id']:r for r in service._source_eligibility(current, {})}
    identities = {r['page_id']:r for r in service._registry()['subjects'] if r['status']=='active'}
    accepted = {}
    for row in scopes.values():
        for item in row['closure']['pages']:
            accepted[item['page']['page_id']] = row, item['page']
    runs = {}
    for ref in base['build_runs']:
        rid = ref['run_id']
        run = _load_json(service.build._run_path(rid))
        candidates, _ = service.build._pass_index([t['task_id'] for t in run['tasks']])
        runs[rid] = run, candidates, {d['page_id']:d for d in run['decisions']}
        if run['state'] == 'completed':
            if run['revision'] != ref['revision']:
                fail('completed Build revision changed')
            service.build.validate_run(rid)
            proof = {'completed_run':rid, 'manifest':pin(service.vault,
                service.build._run_path(rid).relative_to(service.vault).as_posix())}
            for page in run['page_revisions']:
                if service._page_revision(page['page_id'],page['revision_id']) != page:
                    fail('completed Build page differs from its committed revision')
                if page['page_id'] in accepted and accepted[page['page_id']][1] != page:
                    fail('Build acceptance names conflicting revisions')
                accepted[page['page_id']] = proof, page
    # Both native partial closures and fully committed Builds are exact bases.
    # No synthetic closure or change to original page governance is needed.
    subjects = ([identities[pid] for pid in sorted(accepted) if pid in identities]
                if initial else base['subjects'])
    rows = []
    with span('page_governance_preflight', vault=service.vault):
        for subject in subjects:
            pid = subject['page_id']
            page = service._page_revision(pid, subject['current_revision_id'])
            reasons = []
            issues = []
            identity = identities.get(pid, {})
            if (identity.get('current_revision_id') != page['revision_id']
                    or identity.get('current_path') != page['path']):
                reasons.append('page-identity-not-current')
            from .release_reads import body
            if _sha(body(_vault_path(service.vault,page['path']))) != page['authored_sha256']:
                fail('committed page body changed')
            bound = accepted.get(pid)
            if not bound or bound[1] != page:
                reasons.append('missing-exact-delivery-acceptance')
            review = page.get('review') or {}
            acceptance = review.get('acceptance')
            if not acceptance:
                reasons.append('missing-program-acceptance')
            if bound:
                run_id = bound[0].get('completed_run') or next(
                    r['run_id'] for r in bound[0]['closure']['pages'] if r['page']['page_id']==pid)
                run, candidates, decisions = runs[run_id]
                selected = [candidates[(r['pass_id'],r['candidate_id'])] for r in decisions[pid]['candidate_refs']]
                issues = qa_issues(service,page,selected) if page['qa_status']!='usable' else []
                if acceptance:
                    from .build_acceptance import validate_acceptance
                    try:
                        validate_acceptance(service.build,run,page,review,selected=selected)
                    except ContractError as exc:
                        reasons.append('acceptance-no-longer-valid:'+exc.code)
            if page['qa_status'] != 'usable':
                reasons.append('qa-required')
            if not page['support_refs'] or any(not source.get(r['unit_ref']['resource_id'],{}).get('eligible')
                   or service._contribution(r,current,{})!='active'
                   for r in page['support_refs']):
                reasons.append('source-governance-ineligible')
            evidence = list(exclusion_evidence)
            if bound:
                row, _ = bound
                evidence.extend([row['manifest']] if row.get('completed_run')
                            else [row['pointer'], row['closure']['authorization']])
                # Native closure already validates exact acceptance. Pin the
                # unchanged inputs that made this acceptance reusable.
                if not row.get('completed_run'):
                    evidence.extend(r['manifest'] for r in row['scope']['runs'])
                for citation, _ in selected:
                    evidence.append(pin(service.vault,
                        f"_system/knowledge-builds/task-{citation['task_id']}/passes/{citation['sequence']:04d}-{citation['pass_id']}.json"))
            side = f"_system/knowledge-builds/page-revisions/{pid}/{page['revision_id']}.json"
            rows.append({'page_id':pid,'revision_id':page['revision_id'],
                'authored_sha256':page['authored_sha256'],'page_revision':pin(service.vault,side),
                'support_digest':fingerprint(page['support_refs']),
                'acceptance_digest':fingerprint(acceptance), 'evidence':evidence,
                'qa_status':page['qa_status'], 'business_status':'unassessed' if reasons else 'approved',
                'visibility':'draft' if reasons else 'published',
                'eligible':not reasons,'reasons':sorted(set(reasons)), 'qa_issues':issues,
                'qa_disposition':'isolated; no semantic retry' if page['qa_status']!='usable' else 'unchanged-usable'})
    record = {'contract':'hermes-page-governance/v1','authorization':authorization_pointer,
        'actor':q['actor'],'workflow_id':q['workflow_id'],'retrieval_scope':q['retrieval_scope'],
        'policy':POLICY,'policy_sha256':fingerprint(POLICY),'base_release':q['base_release'],
        'pages':rows,'new_model_calls':0}
    record['governance_id'] = fingerprint(record)
    validate_record('page_governance', record)
    path = _vault_path(service.vault,f"{ROOT}/{record['governance_id']}.json")
    if path.exists() and _load_json(path)!=record:
        fail('governance identity changed')
    if not path.exists():
        _write_atomic(path,_json_bytes(record))
    return pin(service.vault,path.relative_to(service.vault).as_posix())


def record(vault, pointer):
    from .release_reads import current
    session = current(_vault_path(vault,pointer['ref']))
    key = ('governance',pointer['ref'],pointer['sha256'])
    if session is not None and key in session.proofs:
        return session.proofs[key]
    value = pinned(vault,pointer)
    validate_record('page_governance',value)
    if (value['policy']!=POLICY or value['policy_sha256']!=fingerprint(POLICY)
            or value['governance_id']!=fingerprint({k:v for k,v in value.items() if k!='governance_id'})):
        fail('governance policy or identity changed')
    grant = authorization(vault,value['authorization'])['request']
    if any(value[k]!=grant[k] for k in ('actor','workflow_id','retrieval_scope','base_release')):
        fail('governance record escaped authorization')
    if session is not None:
        session.proofs[key]=value
        session.proofs[('governance-pages',pointer['sha256'])]={r['page_id']:r for r in value['pages']}
    return value


def effective(service, page, pointer=None):
    if pointer is None:
        return {k:page[k] for k in ('qa_status','business_status','visibility')}
    value = record(service.vault,pointer)
    if value['retrieval_scope']!=service.source.vault_manifest['vault']['id']:
        fail('foreign retrieval scope')
    from .release_reads import current
    session = current(_vault_path(service.vault,pointer['ref']))
    matches = ([session.proofs[('governance-pages',pointer['sha256'])].get(page['page_id'])]
               if session else [r for r in value['pages'] if r['page_id']==page['page_id']])
    if len(matches)!=1 or matches[0] is None:
        fail('page absent from exact governance snapshot')
    row = matches[0]
    if (row['revision_id']!=page['revision_id'] or row['authored_sha256']!=page['authored_sha256']
            or row['support_digest']!=fingerprint(page['support_refs'])
            or row['acceptance_digest']!=fingerprint((page.get('review') or {}).get('acceptance'))
            or pinned(service.vault,row['page_revision'])!=page):
        fail('governance content or acceptance changed')
    for evidence in row['evidence']:
        from .release_reads import body
        if _sha(body(_vault_path(service.vault,evidence['ref'])))!=evidence['sha256']:
            fail('governance support or delivery evidence changed')
    if row['qa_status']!=page['qa_status'] or (row['eligible'] and (row['reasons'] or page['qa_status']!='usable')):
        fail('governance cannot clear source QA')
    return {k:row[k] for k in ('qa_status','business_status','visibility')}


def reasons(service, page, pointer=None):
    state = effective(service,page,pointer)
    return [reason for key,needed,reason in (
        ('qa_status','usable','qa-required'),('business_status','approved','business-unassessed'),
        ('visibility','published','visibility-draft')) if state[key]!=needed]


def preflight(plan):
    counts = {}
    for row in plan['index_eligibility']:
        if row['kind']=='knowledge_page':
            for reason in row['reasons']:
                counts[reason]=counts.get(reason,0)+1
    return {'contract':'page-governance-preflight/v1','release_id':plan['release_id'],
        'included':len(plan['subjects']),'excluded':len(plan.get('delivery',{}).get('excluded',[])),
        'index_eligible':sum(r['eligible'] for r in plan['index_eligibility'] if r['kind']=='knowledge_page'),
        'missing_conditions':counts,'provider_executed':False,
        'isolated':plan.get('delivery',{}).get('excluded',[])}


def validate_retrieval_snapshot(service, release):
    """Provider admission shares the plan's effective governance rules."""
    current = service._current_sets()
    source = {r['resource_id']:r for r in service._source_eligibility(current,{})}
    for item in release['index_eligibility']:
        if item['kind']!='knowledge_page' or not item['eligible']:
            continue
        page = service._page_revision(item['id'],item['page_revision_id'])
        if reasons(service,page,release.get('page_governance')):
            fail('eligible retrieval snapshot has unresolved page conditions')
        if any(not source.get(r['unit_ref']['resource_id'],{}).get('eligible')
               or service._contribution(r,current,{})!='active' for r in page['support_refs']):
            fail('retrieval snapshot sources are no longer qualified')
