"""Invocation-local schema reuse retains every record check and retry boundary."""
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hermes-source-units/src'))
sys.path.insert(0,str(ROOT/'hermes-obsidian-controlled-ingest/lib'))
from hermes_source_units import validation as v
import ingest_kanban as k

def test_schema_audited_once_but_bad_record_still_rejected(monkeypatch):
    loads=[];audits=[]
    load=v.load_schema; audit=v.check_schema
    monkeypatch.setattr(v,'load_schema',lambda:loads.append(1) or load())
    monkeypatch.setattr(v,'check_schema',lambda s:audits.append(1) or audit(s))
    with v.record_validation_scope():
        v.validate_record('config',v.default_config())
        with pytest.raises(v.ContractError):
            v.validate_record('config',{'bad':'record'})
        v.validate_record('config',v.default_config())
    assert len(loads)==len(audits)==1
    v.validate_record('config',v.default_config())
    assert len(loads)==len(audits)==2

def test_scope_is_discarded_after_failure(monkeypatch):
    with pytest.raises(v.ContractError),v.record_validation_scope():
        v.validate_record('config',{})
    assert v._RECORD_SCHEMA.get() is None

def test_watcher_single_sync_attempt_returns_contention_to_refill(monkeypatch):
    adapter=object.__new__(k.IngestKanbanAdapter)
    adapter.workflow=SimpleNamespace(status=lambda wid:{'workflow_id':wid,'actor':'a','revision':1})
    attempts=[]
    def sync(*a,**kw):
        attempts.append(1)
        raise v.ContractError('LOCK_BUSY','$','busy')
    monkeypatch.setattr(adapter,'sync',sync)
    with pytest.raises(v.ContractError,match='LOCK_BUSY'):
        adapter._sync_current('ingest-test',max_attempts=1,yield_requested=lambda:False)
    assert len(attempts)==1

def test_wake_yields_before_full_projection():
    adapter=object.__new__(k.IngestKanbanAdapter)
    with pytest.raises(v.ContractError,match='RECONCILIATION_YIELD'):
        adapter._sync_locked({},yield_requested=lambda:True)
