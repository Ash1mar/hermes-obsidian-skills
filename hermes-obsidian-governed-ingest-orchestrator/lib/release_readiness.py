"""Read-only compatibility inventory; ready is never worker completion."""
from release_execution_migration import KINDS
from ingest_kanban import executor_for
def readiness(adapter, workflow_id):
    w=adapter.workflow.status(workflow_id)
    templates=adapter.workflow.pinned_templates(w)
    rows=[{'node':k,'executor':executor_for(k,templates[k]),
           'compatible':executor_for(k,templates[k])=='program-v1'} for k in KINDS]
    return {'ok':all(r['compatible'] for r in rows),'workflow_id':workflow_id,
            'revision':w['revision'],'state':w['state'],'nodes':rows,
            'reason':None if all(r['compatible'] for r in rows) else 'RELEASE_EXECUTOR_MISMATCH',
            'resumed':False,'workers_started':0}
