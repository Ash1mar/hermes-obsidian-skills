"""Conservative release impact classification; raw full-Vault diagnostics survive."""
from .source_units import _vault_path,_load_json,_sha
from .validation import validate_record

def impact_context(service,plan,lint):
    """Prove optional legacy diagnostics cannot supply this release's evidence."""
    selected_runs={r['run_id'] for r in plan['build_runs']}
    prior=plan['delivery'].get('base_release')
    if prior:selected_runs.update(r['run_id'] for r in _load_json(_vault_path(service.vault,prior['ref']))['build_runs'])
    registry=service._registry()
    owners={s['created_by_run'] for s in registry['subjects'] if s['status']=='active'}
    resources={ref['unit_ref']['resource_id'] for s in plan['subjects'] for ref in s['active_support_refs']}
    for resource in sorted(resources):service.source.validate(resource)
    obsolete={};empty=[]
    for issue in lint.get('issues',[]):
        code=issue.get('code');ref=issue.get('path','');path=_vault_path(service.vault,ref)
        if (code=='source_units.knowledge_build_invalid' and issue.get('message','').startswith('COVERAGE_SCOPE_INVALID')
                and ref.startswith('_system/knowledge-builds/') and path.is_file()):
            run=_load_json(path);validate_record('build_run',run)
            if (run['run_id']==path.parent.name and run['state']=='draft' and run['run_id'] not in selected_runs|owners):
                obsolete[ref]=_sha(path.read_bytes())
        if (resources and code=='bundle.missing_control_file' and ref.startswith('10_Raw/converted/')
                and path.is_dir() and not any(p.is_file() for p in path.rglob('*'))):empty.append(ref)
    return {'validated_resources':sorted(resources),'unowned_draft_runs':obsolete,'empty_legacy_bundle_dirs':sorted(empty)}

def classify(plan, lint, context=None):
    selected={s['current_path'] for s in plan['subjects']}
    excluded={e['path'] for e in plan['delivery']['excluded']}
    local=('frontmatter.', 'evidence.', 'semantic.', 'markdown.')
    rows=[];context=context or {}
    for issue in lint.get('issues',[]):
        path=issue.get('path','');code=issue.get('code','')
        unrelated=path in excluded and path not in selected and code.startswith(local)
        reason='excluded-page-local' if unrelated else 'selected-shared-or-unclassified'
        if (code=='source_units.knowledge_build_invalid' and path in context.get('unowned_draft_runs',{})
                and issue.get('message','').startswith('COVERAGE_SCOPE_INVALID')):
            unrelated=True;reason='unowned-unselected-historical-draft-scope'
        if code=='bundle.missing_control_file' and path in context.get('empty_legacy_bundle_dirs',[]):
            unrelated=True;reason='empty-legacy-container-outside-validated-sourceunits'
        rows.append({**issue,'release_blocking':issue.get('severity')=='error' and not unrelated,
                     'impact_reason':reason})
    raw_ok=lint.get('whole_vault_ok',lint.get('ok',False))
    return {**lint,'ok':(bool(raw_ok) or bool(rows)) and not any(r['release_blocking'] for r in rows),
            'issues':rows,'whole_vault_ok':raw_ok,'impact_context':context,
            'release_summary':{'errors':sum(r['release_blocking'] for r in rows),
                               'diagnostic_errors':sum(r.get('severity')=='error' and not r['release_blocking'] for r in rows)}}
