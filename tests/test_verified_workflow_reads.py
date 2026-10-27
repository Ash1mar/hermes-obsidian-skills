"""Fresh byte proofs, mutation isolation and unchanged workflow stop semantics."""
import copy,hashlib,json,os
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.verified_json import VerifiedJsonReads
from hermes_source_units.ingest_workflow import FileIngestWorkflowService
import hermes_source_units.ingest_workflow as workflow
import fixed_history


def test_same_bytes_reuse_validation_but_same_mtime_drift_does_not(tmp_path):
    path=tmp_path/'record.json';path.write_text('{"value":1}')
    checks=[]
    def validate(kind,value):checks.append(copy.deepcopy(value))
    cache=VerifiedJsonReads()
    first=cache.read(path,kind='fixture',validator=validate);first['value']=99
    assert cache.read(path,kind='fixture',validator=validate)=={'value':1}
    assert len(checks)==1
    stamp=path.stat().st_mtime_ns;path.write_text('{"value":2}');os.utime(path,ns=(stamp,stamp))
    assert cache.read(path,kind='fixture',validator=validate)=={'value':2} and len(checks)==2
    other=tmp_path/'other.json';other.write_bytes(path.read_bytes())
    cache.read(other,kind='fixture',validator=validate);assert len(checks)==3


def test_invalid_changed_record_never_reuses_old_valid_shape(tmp_path):
    path=tmp_path/'record.json';path.write_text('{"ok":true}')
    def validate(_kind,value):
        if value.get('ok') is not True:raise ContractError('INVALID_SCHEMA','$','fixture')
    cache=VerifiedJsonReads();cache.read(path,kind='fixture',validator=validate)
    path.write_text('{"ok":null}')
    with pytest.raises(ContractError,match='INVALID_SCHEMA'):cache.read(path,kind='fixture',validator=validate)


def test_schema_drift_fails_closed_during_scope(tmp_path,monkeypatch):
    from hermes_source_units import validation
    (tmp_path/'schemas').mkdir();schema=tmp_path/'schemas/contracts.json';schema.write_text('{}')
    monkeypatch.setattr(validation,'__file__',str(tmp_path/'validation.py'))
    path=tmp_path/'record.json';path.write_text('{}');cache=VerifiedJsonReads()
    validate=lambda *_:None;cache.read(path,kind='fixture',validator=validate)
    schema.write_text('[]')
    with pytest.raises(ContractError,match='schema changed'):cache.read(path,kind='fixture',validator=validate)


def test_workflow_status_sees_cancel_without_revision_bump_and_scope_expires(tmp_path,monkeypatch):
    checks=[];wid='ingest-fixture'
    def validate(_kind,value):checks.append(value['cancel_requested'])
    monkeypatch.setattr(workflow,'validate_record',validate)
    service=object.__new__(FileIngestWorkflowService);service.vault=tmp_path
    path=service._path(wid);path.parent.mkdir(parents=True)
    state={'workflow_id':wid,'revision':1,'cancel_requested':False};path.write_text(json.dumps(state))
    with service.read_projection():
        a=service.status(wid);a['cancel_requested']=True
        assert service.status(wid)['cancel_requested'] is False and len(checks)==1
        state['cancel_requested']=True;path.write_text(json.dumps(state))
        assert service.status(wid)['cancel_requested'] is True and len(checks)==2
    assert service._verified_json_reads is None
    service.status(wid);assert len(checks)==3


def test_history_index_preserves_scope_order_copies_and_all_pointer_hashes(tmp_path):
    scope='fixture/v1';node='build-finalize';pointers=[]
    for n in range(2):
        path=tmp_path/f'report-{n}.json'
        path.write_text(json.dumps({'request':{'recovery_scope':scope},'entries':[
            {'node':node,'value':n},{'node':'unrelated','value':100}]}))
        pointers.append({'report_ref':path.name,'report_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    service=NS(vault=tmp_path,_verified_json_reads=VerifiedJsonReads(),
        status=lambda _:{'execution_recovery_history':copy.deepcopy(pointers)})
    adapter=NS(workflow=service);binding={'workflow_id':'fixture','node':node}
    first=list(fixed_history.entries(adapter,binding,scopes=(scope,)))
    first[0][1]['value']=999
    assert [e['value'] for _,e in fixed_history.entries(adapter,binding,scopes=(scope,),reverse=True)]==[1,0]
    assert service._verified_json_reads.stats['parses']==2
    assert list(fixed_history.entries(adapter,binding,scopes=('other',)))==[]
    (tmp_path/'report-0.json').write_text('{}')
    with pytest.raises(ContractError):list(fixed_history.entries(adapter,binding,scopes=('other',)))


def test_new_report_pointer_is_not_hidden_by_existing_index(tmp_path):
    path=tmp_path/'report.json';path.write_text('{"request":{"recovery_scope":"s"},"entries":[]}')
    pointers=[];service=NS(vault=tmp_path,_verified_json_reads=VerifiedJsonReads(),
        status=lambda _:{'execution_recovery_history':copy.deepcopy(pointers)})
    adapter=NS(workflow=service);binding={'workflow_id':'fixture','node':'build-finalize'}
    assert list(fixed_history.entries(adapter,binding))==[]
    path.write_text('{"request":{"recovery_scope":"s"},"entries":[{"node":"build-finalize","value":1}]}')
    pointers.append({'report_ref':path.name,'report_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    assert list(fixed_history.entries(adapter,binding))[0][1]['value']==1
