"""Append native local content, preserve predecessors and require actual review."""
import copy
import pytest
from test_partial_finalize import ready,vault,writer
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.validation import fingerprint
from hermes_source_units.build_acceptance import assess,record,validate_acceptance
from hermes_source_units.partial_finalize import pin,pinned
from hermes_source_units.finalize_revision_state import append,work_id,publish_revision,effective_run

def authored(ready,vault):
    service,run,reviews,req=ready;page=run['page_revisions'][1]
    folder=vault/'_system/ledgers/ingest-workflows/fixture/semantic-calls';folder.mkdir(parents=True,exist_ok=True)
    body='# Pump 1\nCorrected, bounded native content.\n'
    ap=folder/'body.json';ap.write_bytes(_json_bytes({'native_task_id':'native','node':'build-finalize','phase':'page-write',
        'response_sha256':'a'*64,'model':{'origin':'hermes_native_model','response_sha256':'a'*64}}))
    request_path=vault/'_system/reports/local-request.json';request_path.write_bytes(_json_bytes({'view':{'path':page['path']}}))
    audit=pin(vault,ap.relative_to(vault).as_posix());request=pin(vault,request_path.relative_to(vault).as_posix())
    work={'work_id':work_id(run['run_id'],page['page_id']),'run_id':run['run_id'],'page_id':page['page_id'],
        'original_revision_id':page['revision_id'],'latest_revision':None,'state':'revision_authorized',
        'grants':[{'grant_id':'local','calls':{'page-write':{'state':'valid','audit':audit,'request':request,'response':{'content':body}}}}]}
    append(vault,'fixture',work,'authorize-local','revise',{})
    return service,run,reviews,page,work,body,audit,request

def test_local_native_body_preserves_old_bytes_and_cannot_self_approve(ready,vault):
    service,run,reviews,page,work,body,audit,request=authored(ready,vault)
    original=service._run_path(run['run_id']).read_bytes()
    original_body=(service._run_path(run['run_id']).parent/'pages'/f"{page['page_id']}.md").read_bytes()
    with writer(service) as binding:revision=publish_revision(service,binding,work,body,audit,request)
    envelope=pinned(vault,revision)
    assert envelope['predecessor_revision_id']==page['revision_id']
    assert envelope['page']['revision_id']!=page['revision_id']
    assert service._run_path(run['run_id']).read_bytes()==original
    assert (service._run_path(run['run_id']).parent/'pages'/f"{page['page_id']}.md").read_bytes()==original_body
    work.update(latest_revision=revision,state='revision_authored')
    append(vault,'fixture',work,'body-saved','revision',{})
    effective=effective_run(service,run);new=effective['page_revisions'][1]
    assessment=assess(service,effective,new)
    with pytest.raises(ContractError,match='independent native approval'):
        validate_acceptance(service,effective,new,{'acceptance':record(assessment)})
    review_ref='_system/ledgers/ingest-workflows/fixture/semantic-calls/review.json'
    (vault/review_ref).write_bytes(_json_bytes({'contract':'hermes-fixed-semantic-call/v1','workflow_id':'fixture',
        'node':'build-finalize','phase':'page-review','response_sha256':'b'*64,
        'model':{'origin':'hermes_native_model','response_sha256':'b'*64}}))
    work.update(state='revision_reviewed',review_result={'decision':'approved'},review_audit=pin(vault,review_ref))
    work['grants'][-1]['calls']['page-review']={'state':'valid','audit':work['review_audit'],'response':{'decision':'approved'}}
    append(vault,'fixture',work,'review-saved','reviewed',{})
    reviews[1]={'page_id':new['page_id'],'authored_sha256':new['authored_sha256'],'parent_authored_sha256':None,
        'actor':'agent','note':'Actual isolated native review','acceptance':record(assessment,review_audit_ref=review_ref)}
    result=service.finalize({'run_id':run['run_id'],'actor':'agent','expected_revision':1,'reviews':reviews})
    assert result['run']['state']=='completed'
    assert service.validate_run(run['run_id'])['state']=='completed'
    assert (vault/new['path']).read_text()==body
    assert (service._run_path(run['run_id']).parent/'pages'/f"{page['page_id']}.md").read_bytes()==original_body

def test_program_cannot_substitute_manual_content_for_a_native_body_result(ready,vault):
    service,run,reviews,page,work,body,audit,request=authored(ready,vault)
    with writer(service) as binding,pytest.raises(ContractError,match='body bytes differ'):
        publish_revision(service,binding,work,'Manually substituted content',audit,request)
