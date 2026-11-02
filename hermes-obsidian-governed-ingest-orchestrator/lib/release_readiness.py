"""Read-only compatibility inventory; ready is never worker completion."""
from release_execution_migration import KINDS
from ingest_kanban import executor_for
def readiness(adapter, workflow_id, *, governance=False):
    w=adapter.workflow.status(workflow_id)
    templates=adapter.workflow.pinned_templates(w)
    rows=[{'node':k,'executor':executor_for(k,templates[k]),
           'compatible':executor_for(k,templates[k])=='program-v1'} for k in KINDS]
    from hermes_source_units.vault_finalize import FileVaultFinalizeService
    p4=FileVaultFinalizeService(adapter.workflow.vault)
    resources={o['resource_id'] for o in w.get('source_outcomes',[]) if o['status']=='ready'}
    sources=([e for e in p4._source_eligibility(p4._current_sets(),{}) if e['resource_id'] in resources] if governance else [])
    return {'source_governance':{'eligible':sum(e['eligible'] for e in sources),'total':len(sources),'sources':sources},'ok':all(r['compatible'] for r in rows),'workflow_id':workflow_id,
            'revision':w['revision'],'state':w['state'],'nodes':rows,
            'reason':None if all(r['compatible'] for r in rows) else 'RELEASE_EXECUTOR_MISMATCH',
            'resumed':False,'workers_started':0}
