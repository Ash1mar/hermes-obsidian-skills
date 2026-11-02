"""Qualification is additive; missing evidence and source QA stay closed."""
import copy
from pathlib import Path
from types import SimpleNamespace as NS
import json
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hermes-source-units/src'))
from hermes_source_units import ContractError
from hermes_source_units.page_governance import POLICY, effective, qa_issues, create
from hermes_source_units.partial_finalize import pin
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _json_bytes
from hermes_source_units.release_reads import operation, body
from hermes_source_units.ingest_pauses import WorkflowPauseMixin


def test_operation_reuses_bytes_but_freshly_rejects_same_length_change(tmp_path):
    p=tmp_path/'page.md';p.write_bytes(b'old')
    with operation(tmp_path,'test') as reads:
        assert body(p)==body(p)==b'old'
        assert reads.stats['reads']==1 and reads.stats['reuse_hits']==1
        p.write_bytes(b'new')
        with pytest.raises(ContractError,match='SOURCE_CHANGED'):reads.confirm()
    with operation(tmp_path,'test'):
        assert body(p)==b'new'


def test_release_reads_never_cache_workflow_authority(tmp_path):
    p=tmp_path/'_system/ledgers/control.json';p.parent.mkdir(parents=True);p.write_bytes(b'old')
    with operation(tmp_path,'test') as reads:
        assert body(p)==b'old'
        p.write_bytes(b'new')
        assert body(p)==b'new' and reads.records=={}


def test_added_release_pause_blocks_before_latch_and_legacy_policy_is_unchanged():
    s=WorkflowPauseMixin()
    w={'scope':{'pause_after':['checkpoint_2','release_sync'],'provider':'sync'},'batch_id':'batch',
       'cancel_requested':False,'current_stage':'indexing','dispatch_policy':{},
       'pause_control':{'history':[{'boundary':'checkpoint_2'}]},
       'release_refresh_history':[{'input_digest':'sha256:'+'a'*64}]}
    original=copy.deepcopy(w['scope'])
    assert s.pending_pause(w)=='release_apply'
    assert w['scope']==original
    with pytest.raises(ContractError,match='WORKFLOW_PAUSED'):s.assert_not_paused(w)
    w['pause_control']['history'].append({'boundary':'release_apply','release_generation':'sha256:'+'a'*64})
    assert s.pending_pause(w) is None
    w['release_refresh_history'].append({'input_digest':'sha256:'+'b'*64})
    assert s.pending_pause(w)=='release_apply'


def test_qa_inventory_deduplicates_exact_source_check_without_clearing_it():
    check={'qa':'needs-qa','note':'unreadable formula','source_ref':{'span':None}}
    record={'pass_id':'a'*64,'inspections':[check,check]}
    issues=qa_issues(None,None,[(record,{})])
    assert len(issues)==1 and json.loads(issues[0]['check_json'])==check


def test_missing_governance_retains_old_draft_conditions():
    page={'qa_status':'usable','business_status':'unassessed','visibility':'draft'}
    assert effective(None,page)==page


def test_governance_writer_requires_native_authority(tmp_path):
    request={'policy':POLICY,'actor':'operator','workflow_id':'ingest-fixture','automatic_page_approval':True,
             'stop_after':'release_apply'}
    request['input_digest']='sha256:'+fingerprint(request)
    p=tmp_path/'grant.json';p.write_bytes(_json_bytes({'request':request}))
    with pytest.raises(ContractError,match='bound native'):
        create(NS(vault=tmp_path),pin(tmp_path,'grant.json'))


def test_old_validation_binding_requires_authenticated_published_generation(vault):
    from test_p4_vault_finalize import completed_page, request, apply
    from test_p3_knowledge_build import publish_sources
    from hermes_source_units.vault_finalize import FileVaultFinalizeService
    from hermes_source_units.page_governance import historical_plan_ids
    refs=publish_sources(vault,['# Pump\nTransfers coolant.\n'])
    completed_page(vault,refs)
    p4=FileVaultFinalizeService(vault)
    plan=p4.plan(request('prior',['run-initial']))['plan'];apply(p4,plan)
    base=pin(vault,p4._manifest_path('prior').relative_to(vault).as_posix())
    q={'policy':POLICY,'actor':'operator','workflow_id':'ingest-fixture','operation_id':'upgrade',
       'automatic_page_approval':True,'stop_after':'release_apply','base_release':base,
       'retrieval_scope':p4.source.vault_manifest['vault']['id']}
    q['input_digest']='sha256:'+fingerprint(q)
    grant={'request':q,'previous_release':base,'previous_checkpoint':{'state':'approved'}}
    path=vault/'grant.json';path.write_bytes(_json_bytes(grant))
    h={'operation_id':'upgrade','input_digest':q['input_digest'],'authorization':pin(vault,'grant.json')}
    w={'workflow_id':'ingest-fixture','actor':'operator','release_plan_id':None,'release_refresh_history':[h]}
    service=NS(vault=vault,knowledge=NS(source=p4.source))
    assert plan['plan_id'] in historical_plan_ids(service,w)
    changed=copy.deepcopy(w);changed['release_refresh_history'][0]['input_digest']='sha256:'+'0'*64
    with pytest.raises(ContractError,match='authority changed'):historical_plan_ids(service,changed)
