"""Pure schema reuse does not reuse live records or outlive one sync."""
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hermes-source-units/src'))
sys.path.insert(0,str(ROOT/'hermes-obsidian-controlled-ingest/lib'))
import ingest_kanban as k
from hermes_source_units import validation as v

@pytest.mark.parametrize('error',[None,'LOCK_BUSY','RECONCILIATION_YIELD'])
def test_sync_schema_reuse_ends_even_when_projection_is_interrupted(tmp_path,monkeypatch,error):
    for name in ('HERMES_KANBAN_TASK','HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setattr(k,'_exclusive_lock',lambda *a,**kw:nullcontext())
    load=v.load_schema;loads=[];reads=[]
    monkeypatch.setattr(v,'load_schema',lambda:loads.append(1) or load())
    adapter=object.__new__(k.IngestKanbanAdapter)
    def live(wid):
        reads.append(wid)
        v.validate_record('config',v.default_config())
        return {'kanban':{'board_id':None}}
    adapter.workflow=SimpleNamespace(vault=tmp_path,_check_request=lambda r:None,status=live,execution_projection=lambda:nullcontext())
    adapter.kanban=SimpleNamespace()
    def project(request,**kwargs):
        v.validate_record('config',v.default_config())
        with pytest.raises(v.ContractError):v.validate_record('config',{'invalid':'current record'})
        if error:raise v.ContractError(error,'$','interrupted projection')
        return {'task_count':0}
    monkeypatch.setattr(adapter,'_sync_locked',project)
    for count in (1,2):
        if error:
            with pytest.raises(v.ContractError,match=error):adapter.sync({'workflow_id':'ingest-scope'})
        else:assert adapter.sync({'workflow_id':'ingest-scope'})['task_count']==0
        assert len(loads)==count and len(reads)==count
        assert v._RECORD_SCHEMA.get() is None
