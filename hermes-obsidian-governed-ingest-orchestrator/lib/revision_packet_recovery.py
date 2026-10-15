"""Authenticate a failed revision's observed bounded packet before one corrected retry."""
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path
from hermes_source_units.validation import fingerprint
from native_pass_identity import load_context

SCOPE='revision-citation-packet/v1'
CAUSES={
    'ACCESS_DENIED':'ACCESS_DENIED at $: ACCESS_DENIED at $: typed submission requires a bounded model slice',
    'STALE_INPUT':'STALE_INPUT at $: STALE_INPUT at $: reviewed candidate identity changed'}


def verify(adapter,workflow,item,binding):
    revision=workflow['pass_revision'];service=adapter.workflow.knowledge
    targets={t['task_id']:t for t in revision['tasks']}
    owned=[tid for tid in item['task_ids'] if tid in targets]
    checked={'batch_id':workflow['batch_id'],'task_ids':owned}
    _,context=load_context(adapter.workflow.vault,{**binding,'worker_id':'ingest-worker-'+binding['task_id']},checked)
    packet=_load_json(_vault_path(adapter.workflow.vault,context['packet_ref']))
    projections=[];batch=service._batch(workflow['batch_id'])
    for index,tid in enumerate(owned,1):
        records=service._pass_index([tid])[1][tid]
        if max(records,key=lambda p:p['sequence'])['pass_id']!=targets[tid]['pass_id']:
            raise ContractError('STALE_INPUT','$','revision predecessor changed or already has a new result')
        package=service._existing_reading_package(service._task(tid),workflow['actor'],batch['document_registry_revision'])
        projection=adapter.workflow.revision_projection(workflow,tid,service.model_input(tid,package))
        candidate=projection['view']['continuation'].get('candidate',{})
        if (not candidate.get('revision_review') or candidate.get('candidate_pass_id')!=targets[tid]['pass_id']
                or context['candidates'].get(f't{index}')!=targets[tid]['pass_id']):
            raise ContractError('STALE_INPUT','$','failed input is not the authorized authored citation revision')
        projections.append(projection)
    current=service.model_packet(projections,workflow['batch_id'],persist=False)['view']
    if fingerprint(current)!=fingerprint(packet):
        raise ContractError('STALE_INPUT','$','current required revision evidence differs from the observed failed packet')
    return {'packet_ref':context['packet_ref'],'packet_sha256':context['packet_sha256'],
            'revision_id':revision['revision_id'],'predecessors':context['candidates'],
            'template_sha256':revision['template_sha256'],'task_ids':owned}
