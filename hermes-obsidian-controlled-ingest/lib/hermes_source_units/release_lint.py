"""Conservative release impact classification; raw full-Vault diagnostics survive."""
def classify(plan, lint):
    selected={s['current_path'] for s in plan['subjects']}
    excluded={e['path'] for e in plan['delivery']['excluded']}
    local=('frontmatter.', 'evidence.', 'semantic.', 'markdown.')
    rows=[]
    for issue in lint.get('issues',[]):
        path=issue.get('path','');code=issue.get('code','')
        unrelated=path in excluded and path not in selected and code.startswith(local)
        rows.append({**issue,'release_blocking':issue.get('severity')=='error' and not unrelated,
                     'impact_reason':'excluded-page-local' if unrelated else 'selected-shared-or-unclassified'})
    return {**lint,'ok':bool(lint.get('ok')) or (bool(rows) and not any(r['release_blocking'] for r in rows)),
            'issues':rows,'whole_vault_ok':lint.get('ok',False)}
