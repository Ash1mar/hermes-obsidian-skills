"""Verify additive paused-control provenance without weakening pause hashes."""
from .partial_finalize import pinned
from .validation import fingerprint,ContractError

def verify(service,value,report):
    pointer=report.get('recovery')
    if not pointer:return
    plan=pinned(service.vault,pointer)
    from .ingest_workflow import mutation_digest
    if (plan.get('contract')!='pause-control-recovery/v1'
            or mutation_digest(plan['request'])!=plan['request']['input_digest']
            or plan['request']['workflow_id']!=value['workflow_id']
            or plan['request']['actor']!=value['actor']
            or plan['after_workflow']['pause_control']!=value['pause_control']
            or plan['pause']!={k:v for k,v in report.items() if k!='recovery'}):
        raise ContractError('STALE_INPUT','$','pause control recovery provenance changed')
    old=pinned(service.vault,plan['predecessor'])
    if old['evidence_digest']!='sha256:'+fingerprint(old['evidence']):
        raise ContractError('STALE_INPUT','$','predecessor pause report changed')
    for p in plan['controls'].values():pinned(service.vault,p)
