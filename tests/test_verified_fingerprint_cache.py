"""Fresh-byte-bound canonical values; negative cases never use a real Vault."""
import copy,hashlib,json,os
from pathlib import Path
import pytest
from hermes_source_units import ContractError
from hermes_source_units.verified_json import VerifiedJsonReads
from hermes_source_units.verified_snapshot import VerifiedSnapshot
from hermes_source_units.validation import fingerprint
from hermes_source_units.ingest_workflow import FileIngestWorkflowService
import hermes_source_units.ingest_workflow as workflow
import ingest_graph_reuse as graph
from ingest_kanban import _matching_cards


def test_canonical_builds_follow_fresh_content_versions_not_check_count(tmp_path):
    path=tmp_path/'state.json';value={'workflow_id':'w','revision':1,'cancel_requested':False,
        'kanban':{'task_map':[{'node':'n','task_id':'t','payload':'x'*1000}]}}
    path.write_text(json.dumps(value));stamp=path.stat().st_mtime_ns
    cache=VerifiedJsonReads();checks=[]
    def validate(kind,record):checks.append(copy.deepcopy(record))
    with graph.graph_scope() as scope:
        for _ in range(12):
            s=cache.read(path,kind='fixture',validator=validate,readonly=True,origin='same-boundary')
            assert graph.workflow_fingerprint(s)==fingerprint(value)
        assert cache.stats['reads']==12 and cache.stats['canonical_builds']==1
        assert scope['fingerprint_calls']==1 and scope['fingerprint_cache_hits']==11
        assert cache.stats['full_copies']==1 and cache.stats['full_copy_ns']>0
        value['cancel_requested']=True;path.write_text(json.dumps(value));os.utime(path,ns=(stamp,stamp))
        s=cache.read(path,kind='fixture',validator=validate,readonly=True,origin='same-boundary')
        assert s['cancel_requested'] is True and graph.workflow_fingerprint(s)==fingerprint(value)
        assert cache.stats['canonical_builds']==2 and len(checks)==2 and cache.stats['reads']==13


def test_snapshot_fields_selectors_and_mutable_materialization_cannot_poison_canonical(tmp_path):
    path=tmp_path/'state.json';value={'kanban':{'task_map':[{'node':'n','task_id':'t','idempotency_key':'same'}]},'values':[1,2]}
    path.write_text(json.dumps(value));cache=VerifiedJsonReads()
    s=cache.read(path,readonly=True,origin='fixture');expected=s.canonical_fingerprint
    with pytest.raises(TypeError):s['values']=[999]
    returned=s['values'];returned.append(999)
    cards=_matching_cards(s,'n','t');cards[0]['idempotency_key']='mutated'
    assert _matching_cards(s,'n','t')[0]['idempotency_key']=='same'
    assert s['values']==[1,2] and s.canonical_fingerprint==expected
    def mutate_internal(data):data['values'][0]=999
    with pytest.raises(TypeError):s.select(('attempt-mutation',),mutate_internal)
    mutable=copy.deepcopy(s);mutable['values'].append(3)
    assert type(mutable) is dict and graph.workflow_fingerprint(mutable)!=expected
    mutable['values'].append(4)
    assert graph.workflow_fingerprint(mutable)==fingerprint(mutable) and s.canonical_fingerprint==expected


def test_path_rule_version_and_schema_bind_the_pure_value(tmp_path,monkeypatch):
    from hermes_source_units import validation
    (tmp_path/'schemas').mkdir();schema=tmp_path/'schemas/contracts.json';schema.write_text('{}')
    monkeypatch.setattr(validation,'__file__',str(tmp_path/'validation.py'))
    path=tmp_path/'a.json';path.write_text('{"value":1}');other=tmp_path/'b.json';other.write_bytes(path.read_bytes())
    cache=VerifiedJsonReads();validate=lambda *_:None
    first=cache.read(path,kind='fixture',validator=validate,readonly=True,canonical_version='v1')
    second=cache.read(path,kind='fixture',validator=validate,readonly=True,canonical_version='v2')
    third=cache.read(other,kind='fixture',validator=validate,readonly=True,canonical_version='v1')
    assert first.canonical_fingerprint==second.canonical_fingerprint==third.canonical_fingerprint==fingerprint({'value':1})
    assert len({first.content_key,second.content_key,third.content_key})==3 and cache.stats['canonical_builds']==3
    assert first.content_key[1]==hashlib.sha256(path.read_bytes()).hexdigest()
    schema.write_text('[]')
    with pytest.raises(ContractError,match='schema changed'):cache.read(path,kind='fixture',validator=validate,readonly=True)


def test_fingerprint_remains_lazy_at_its_original_failure_boundary(tmp_path):
    path=tmp_path/'record.json';path.write_text('{"cancel_requested":true,"noncanonical":1.5}')
    cache=VerifiedJsonReads();s=cache.read(path,readonly=True,origin='late-canonical')
    assert s['cancel_requested'] is True and cache.stats['canonical_builds']==0
    with pytest.raises(ContractError,match='INVALID_CANONICAL_VALUE'):graph.workflow_fingerprint(s)
    assert cache.stats['canonical_builds']==0  # no failed verdict is remembered


def test_readonly_service_retains_identity_shape_and_mutable_status_contract(tmp_path,monkeypatch):
    def validate(kind,value):
        if value.get('invalid'):raise ContractError('INVALID_SCHEMA','$','full shape still checked')
    monkeypatch.setattr(workflow,'validate_record',validate)
    service=object.__new__(FileIngestWorkflowService);service.vault=tmp_path
    path=service._path('ingest-fixture');path.parent.mkdir(parents=True)
    value={'workflow_id':'ingest-fixture','values':[1]};path.write_text(json.dumps(value))
    with service.read_projection():
        s=service.status_snapshot('ingest-fixture',origin='fixture.live')
        assert isinstance(s,VerifiedSnapshot)
        mutable=service.status('ingest-fixture');mutable['values'].append(99)
        assert s['values']==[1] and service.status('ingest-fixture')['values']==[1]
        assert 'full:status:test_verified_fingerprint_cache.py:test_readonly_service_retains_identity_shape_and_mutable_status_contract' in '\n'.join(service._verified_json_reads.copy_origins)
        value['invalid']=True;path.write_text(json.dumps(value))
        with pytest.raises(ContractError,match='full shape'):service.status_snapshot('ingest-fixture',origin='fixture.live')
        value.pop('invalid');value['workflow_id']='ingest-other';path.write_text(json.dumps(value))
        with pytest.raises(ContractError,match='identity mismatch'):service.status_snapshot('ingest-fixture',origin='fixture.live')
