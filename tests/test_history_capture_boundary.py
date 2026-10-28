"""A report's hash and interpretation share captured bytes; next query is fresh."""
import hashlib,json,os
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.verified_json import VerifiedJsonReads
import semantic_output


def fixture(tmp_path,reports):
    pointers=[]
    for n,entries in enumerate(reports):
        p=tmp_path/f'r-{n}.json';raw=json.dumps({'entries':entries}).encode();p.write_bytes(raw)
        pointers.append({'report_ref':p.name,'report_sha256':hashlib.sha256(raw).hexdigest()})
    service=NS(vault=tmp_path,_verified_json_reads=VerifiedJsonReads(),
        status=lambda _:{'execution_recovery_history':pointers})
    return NS(workflow=service),{'workflow_id':'fixture','node':'n'},pointers


def test_boundary_reads_each_report_once_keeps_latest_none_and_unrelated_integrity(tmp_path,monkeypatch):
    a,b,pointers=fixture(tmp_path,[[{'node':'n','output_boundary':{'value':1}}],
        [{'node':'other'},{'node':'n','output_boundary':{'value':2}},{'node':'n'}]])
    reads=[];real=Path.read_bytes
    def read(path):reads.append(path.name);return real(path)
    monkeypatch.setattr(Path,'read_bytes',read)
    assert semantic_output.output_boundary(a,b)==(None,pointers[1])
    assert reads==['r-0.json','r-1.json']
    (tmp_path/'r-0.json').write_text('{}')
    with pytest.raises(ContractError,match='recovery evidence changed'):semantic_output.output_boundary(a,b)


def test_replacement_after_capture_cannot_supply_unhashed_interpretation(tmp_path,monkeypatch):
    a,b,_=fixture(tmp_path,[[{'node':'n','output_boundary':{'value':'verified'}}]])
    real=Path.read_bytes;changed=[]
    def read(path):
        data=real(path)
        if path.name=='r-0.json' and not changed:
            changed.append(1);stamp=path.stat().st_mtime_ns
            path.write_text(json.dumps({'entries':[{'node':'n','output_boundary':{'value':'unverified'}}]}))
            os.utime(path,ns=(stamp,stamp))
        return data
    monkeypatch.setattr(Path,'read_bytes',read)
    assert semantic_output.output_boundary(a,b)[0]=={'value':'verified'}
    with pytest.raises(ContractError):semantic_output.output_boundary(a,b)


def test_early_failure_and_report_order_are_preserved(tmp_path):
    a,b,pointers=fixture(tmp_path,[[{'node':'n','output_boundary':{'value':1}}],[]])
    (tmp_path/'r-1.json').unlink()
    with pytest.raises(ContractError):semantic_output.output_boundary(a,b)
    assert a.workflow._verified_json_reads.stats['reads']==1
