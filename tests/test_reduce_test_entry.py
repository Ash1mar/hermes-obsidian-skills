"""Isolated retest admission mechanics; never create native/domain outputs."""
import copy
import hashlib
import json
from types import SimpleNamespace as NS

import pytest
from hermes_source_units import ContractError
from hermes_source_units.source_units import _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint
from reduce_test import _apply, reenter, CONTRACT
from ingest_kanban import node_identity
from hermes_source_units.presentation_contract import pin


def test_downstream_epoch_never_changes_pass_identity():
    w={'workflow_id':'ingest-fixture'}
    active={**w,'reduce_test':{'operation_id':'fixture-cycle'}}
    for kind in ('source-prepare','exact-plan','pass-slice'):
        assert node_identity(w,kind,{},[])==node_identity(active,kind,{},[])
    for kind in ('resource-reduce','global-reduce','build-finalize','provider-sync','acceptance'):
        assert node_identity(w,kind,{},[])!=node_identity(active,kind,{},[])


@pytest.mark.parametrize('interrupt',[False,True])
def test_retirement_replays_without_losing_prior_budget_or_touching_prefix(tmp_path,interrupt):
    old=b'old returned request and cumulative attempts'
    source=tmp_path/'_system/old-page-budget.json'
    prefix=tmp_path/'_system/pass-budget.json'
    _write_atomic(source,old);_write_atomic(prefix,b'unchanged native Pass budget')
    journal=tmp_path/'_system/reduce-tests/cycle/transaction.json'
    plan={'phase':'prepared','retired_files':{'_system/old-page-budget.json':hashlib.sha256(old).hexdigest()},
          'preserved_files':{'_system/pass-budget.json':hashlib.sha256(prefix.read_bytes()).hexdigest()},
          'writes':{'_system/active-boundary.json':'{"state":"cancelled"}'}}
    _write_atomic(journal,_json_bytes(plan))
    if interrupt:
        dest=journal.parent/'retired/_system/old-page-budget.json'
        dest.parent.mkdir(parents=True);source.rename(dest)
    _apply(tmp_path,journal,copy.deepcopy(plan))
    _apply(tmp_path,journal,copy.deepcopy(plan))
    assert not source.exists()
    assert (journal.parent/'retired/_system/old-page-budget.json').read_bytes()==old
    assert prefix.read_bytes()==b'unchanged native Pass budget'
    assert json.loads(journal.read_text())['phase']=='admitted'


def test_retirement_rejects_changed_preimage(tmp_path):
    p=tmp_path/'page.json';p.write_bytes(b'changed')
    plan={'retired_files':{'page.json':hashlib.sha256(b'old').hexdigest()},'writes':{},'preserved_files':{}}
    with pytest.raises(ContractError,match='changed'):
        _apply(tmp_path,tmp_path/'audit/transaction.json',plan)
    assert p.read_bytes()==b'changed'


def test_test_pin_accepts_only_hash_bound_admission_and_keeps_old_pass_budget(tmp_path):
    root=tmp_path/'_system/ledgers/ingest-workflows/ingest-fixture'
    report={'contract':CONTRACT,'request':{'workflow_id':'ingest-fixture','operation_id':'fixture-cycle'},'new_pass_calls_allowed':0}
    ref='_system/reports/admission.json';raw=_json_bytes(report);_write_atomic(tmp_path/ref,raw)
    w={'workflow_id':'ingest-fixture','scope':{},'page_execution_contract':'unified-page-execution/v3',
       'reduce_test':{'operation_id':'fixture-cycle','report_ref':ref,'report_sha256':hashlib.sha256(raw).hexdigest()}}
    _write_atomic(root/'semantic-calls/accepted-pass.json',_json_bytes({'phase':'citation'}))
    budget=_json_bytes({'tasks':{'original':{'attempts':[{'state':'returned'}]}}})
    _write_atomic(root/'pass-call-budget.json',budget)
    result=pin(NS(vault=tmp_path),w)
    assert result['page_budget_contract']=='page-budget-index/v2'
    assert result['page_execution_contract']=='unified-page-execution/v3'
    assert (root/'pass-call-budget.json').read_bytes()==budget
    (tmp_path/ref).write_text('{}')
    with pytest.raises(ContractError,match='admission changed'):pin(NS(vault=tmp_path),w)


@pytest.mark.parametrize('mode,overwrite',[(None,True),(CONTRACT,False)])
def test_ordinary_entry_never_rewinds(tmp_path,mode,overwrite):
    service=NS(vault=tmp_path)
    with pytest.raises(ContractError,match='explicit'):
        reenter(NS(workflow=service),{'workflow_id':'ingest-fixture','test_mode':mode,'overwrite_downstream':overwrite})


def test_workers_cannot_authorize_retest(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK','isolated-worker')
    with pytest.raises(ContractError,match='trusted'):
        reenter(NS(workflow=NS(vault=tmp_path)),{'workflow_id':'ingest-fixture'})


def test_pending_restore_fences_ordinary_resume(tmp_path):
    from hermes_source_units.execution_plan import ExecutionPlanMixin
    w={'workflow_id':'ingest-fixture','reduce_test':{'operation_id':'cycle'}}
    with pytest.raises(ContractError,match='admission'):
        ExecutionPlanMixin.assert_execution_ready(NS(vault=tmp_path),w)
