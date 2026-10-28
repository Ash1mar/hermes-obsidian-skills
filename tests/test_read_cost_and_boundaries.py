"""Read costs, captured-byte identity and unchanged cross-check freshness."""
import copy,hashlib,json,os
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.verified_json import VerifiedJsonReads
from hermes_source_units.verified_snapshot import field_projection
from hermes_source_units.read_accounting import accounting_scope,read_bytes
from hermes_source_units.runtime_telemetry import span
from hermes_source_units.record_snapshot import record_snapshot,snapshot_json
from hermes_source_units.pass_revision import PassRevisionMixin
from hermes_source_units.ingest_pauses import WorkflowPauseMixin


@pytest.mark.parametrize('state',['accepted','running','awaiting_review'])
@pytest.mark.parametrize('scope',['ordinary','reduce-review/v1'])
def test_revision_projection_keeps_decisions_and_copies_no_task_payload(tmp_path,state,scope):
    path=tmp_path/'w.json'
    value={'pass_revision':{'state':state,'revision_scope':scope,'slice_ids':['s'],'boundary':'pass',
        'tasks':[{'task_id':'t','large':'x'*10000}]},'current_stage':'reducing',
        'pause_control':{'boundary':'pass'},'scope':{},'batch_id':'b','cancel_requested':False}
    path.write_text(json.dumps(value));cache=VerifiedJsonReads()
    snap=cache.read(path,readonly=True)
    assert PassRevisionMixin.revision_allows(None,snap,'pass-slice:s')==PassRevisionMixin.revision_allows(None,value,'pass-slice:s')
    assert WorkflowPauseMixin.pending_pause(None,snap)==WorkflowPauseMixin.pending_pause(None,value)
    projected=field_projection(snap,'pass_revision',('state','slice_ids'))
    projected['slice_ids'].append('pollution')
    assert field_projection(snap,'pass_revision',('state','slice_ids'))['slice_ids']==['s']
    assert cache.stats['full_copies']==0 and cache.stats['projected_copy_nodes']<100
    assert not any('[pass_revision]' in key for key in cache.copy_origins)


def test_captured_batch_derivation_reuses_only_given_bytes_and_current_schema(tmp_path,monkeypatch):
    from hermes_source_units import validation
    schema=tmp_path/'schemas/contracts.json';schema.parent.mkdir();schema.write_text('{}')
    monkeypatch.setattr(validation,'__file__',str(tmp_path/'validation.py'))
    path=tmp_path/'batch.json';checks=[];cache=VerifiedJsonReads()
    validator=lambda kind,row:checks.append(row['task_ids'])
    def project(data):
        return cache.captured(path,data,kind='fixture',validator=validator,
            project=lambda value:{'task_ids':value['task_ids']},projection_key=('tasks',),origin='fixture')
    raw=b'{"task_ids":["original"]}'
    path.write_bytes(raw)
    assert project(raw)==project(raw)=={'task_ids':['original']} and len(checks)==1
    path.write_text('{"task_ids":["changed"]}')
    assert project(raw)=={'task_ids':['original']}
    assert project(path.read_bytes())=={'task_ids':['changed']} and len(checks)==2
    assert cache.stats['reads']==0
    schema.write_text('[]')
    with pytest.raises(ContractError,match='schema changed'):project(raw)


def test_read_accounting_separates_real_bytes_captures_sources_and_boundaries(tmp_path):
    path=tmp_path/'r.json';path.write_bytes(b'{}');cache=VerifiedJsonReads()
    with accounting_scope() as ledger:
        for _ in range(2):
            with span('worker_authority_check',vault=tmp_path,workflow_id='fixture'):
                assert cache.read(path)==cache.read(path)=={}
        with record_snapshot({path:(b'{}',hashlib.sha256(b'{}').digest())}):
            assert snapshot_json(path)==(True,{})
        read_bytes(path,'dependency_recheck')
    rows=list(ledger.rows.values());boundaries=list(ledger.boundaries.values())
    assert sum(r['bytes'] for r in rows)==10 and sum(r['reads'] for r in rows)==5
    assert sum(r['captures'] for r in rows)==1
    assert sum(r['bytes'] for r in boundaries)==10
    checked={r['boundary_id'] for r in boundaries if r['boundary']=='worker_authority_check'}
    assert len(checked)==2 and sum(r['duplicate_json_reads'] for r in boundaries)==2


def test_direct_json_newline_semantics_and_capture_do_not_reopen(tmp_path,monkeypatch):
    from hermes_source_units.source_units import _load_json
    path=tmp_path/'r.json';raw=b'{\r\n"value":1\r\n}';path.write_bytes(raw)
    assert _load_json(path)=={'value':1}
    with record_snapshot({path:(raw,hashlib.sha256(raw).digest())}):
        monkeypatch.setattr(type(path),'read_bytes',lambda _:pytest.fail('captured file reopened'))
        assert _load_json(path)=={'value':1}


@pytest.mark.parametrize('change',['same_mtime','delete','symlink'])
def test_each_next_check_observes_report_changes_even_for_unrelated_scope(tmp_path,change):
    import fixed_history
    path=tmp_path/'r.json';raw=json.dumps({'request':{'recovery_scope':'unrelated'},'entries':[]}).encode();path.write_bytes(raw)
    pointers=[{'report_ref':path.name,'report_sha256':hashlib.sha256(raw).hexdigest()}]
    service=NS(vault=tmp_path,_verified_json_reads=VerifiedJsonReads(),status=lambda _:{'execution_recovery_history':pointers})
    adapter=NS(workflow=service);binding={'workflow_id':'fixture','node':'n'}
    def read():return list(fixed_history.journal_rows(adapter,binding,'j','build_review',scopes=('wanted',)))
    assert read()==[]
    if change=='same_mtime':
        stamp=path.stat().st_mtime_ns;path.write_bytes(b'{}');os.utime(path,ns=(stamp,stamp))
    elif change=='delete':path.unlink()
    else:
        path.unlink();outside=tmp_path.parent/(tmp_path.name+'-outside.json');outside.write_bytes(raw)
        path.symlink_to(outside)
    with pytest.raises(ContractError):read()
