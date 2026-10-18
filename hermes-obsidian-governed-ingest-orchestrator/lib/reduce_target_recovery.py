"""Explicit syntax retry and evidence-bound release of derived dependency holds."""
import hashlib
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path
from hermes_source_units.validation import fingerprint


def contract_hash():return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def verify(adapter,value,node,binding,entry,failure,runs,scope):
    if scope=='source-format/v1':
        if node.kind!='resource-reduce':raise ContractError('RECOVERY_NOT_ALLOWED','$','format recovery is source-only')
        from source_hold_recovery import verify as verify_source
        return {**verify_source(adapter,value,node,binding,entry,failure,runs,syntax_only=True),
                'format_retry_limit':1,'original_response_unchanged':True}
    from ingest_kanban import desired_graph,domain_completed
    service=adapter.workflow
    service.verify_pass_skips(value)
    nodes={p.name:p for p in desired_graph(service,value)}
    parent=nodes.get(failure['reason'])
    if parent is None or parent.name not in node.parents or not domain_completed(service,value,parent):
        raise ContractError('DEPENDENCY_EXECUTION_BLOCKED','$','recorded parent is not evidenced complete')
    unresolved=[p for p in node.parents if not domain_completed(service,value,nodes[p])]
    if unresolved:raise ContractError('DEPENDENCY_EXECUTION_BLOCKED','$','other parent dependencies remain incomplete: '+','.join(unresolved))
    evidence=[]
    for name in node.parents:
        p=nodes[name]
        if p.kind=='pass-slice':
            item=service.knowledge._slice(value['batch_id'],name.partition(':')[2])
            active=[t for t in item['task_ids'] if t not in service.skipped_pass_tasks(value)]
            _,records=service.knowledge._pass_index(active)
            citations=[r for rs in records.values() for r in rs if r['pass_kind']=='citation'
                       and not any(n.get('supersedes_pass_id')==r['pass_id'] for n in rs)]
            from fixed_evidence import original_evidence
            for record in citations:
                if fingerprint({k:v for k,v in record.items() if k!='pass_id'})!=record['pass_id']:
                    raise ContractError('SOURCE_CHANGED','$','current citation fingerprint changed')
            original_evidence(service.knowledge,[r for r in citations if r['candidates']],value['actor'],service.knowledge._document_registry_revision())
            evidence.append({'node':name,'slice_sha256':fingerprint(item),'latest_citations':[r['pass_id'] for r in citations],'empty_citations':[r['pass_id'] for r in citations if not r['candidates']]})
        else:evidence.append({'node':name,'input_fingerprint':p.input_fingerprint,'domain_completed':True})
    return {'resolved_dependency':parent.name,'parents_evidence':evidence,
            'accepted_revision_sha256':fingerprint(value.get('pass_revision')),
            'exclusion_history_sha256':fingerprint(value.get('pass_skip_history',[])),
            'parent_work_rerun':False,'derived_failure_only':True}

