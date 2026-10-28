"""Real Linux inode protection and revocation; all damage stays in tmp_path."""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS

import pytest
from hermes_source_units import ContractError
from hermes_source_units import evidence_snapshot as sealed
from hermes_source_units.read_accounting import accounting_scope,read_bytes
from hermes_source_units.source_units import _write_atomic,_load_json
from hermes_source_units.verified_json import VerifiedJsonReads
from ingest_graph_reuse import dependencies,graph_scope,derive,checked_dependencies,recheck_dependencies
import fixed_history
import fixed_semantic_recovery

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Linux kernel sealing contract')


def service_for(vault):
    k=NS(_batch_path=lambda _:vault/'batch.json',_slice_root=lambda _:vault/'inputs/slices',
         _task_path=lambda _:vault/'inputs/task.json',_batch=lambda _:_load_json(vault/'batch.json'))
    return NS(vault=vault,knowledge=k)


def serve(vault,w,absent=False):
    service=service_for(vault);graph=dependencies(service,w)
    if absent:graph['directories'][str(vault/('missing-root/deeper/passes' if absent=='deep' else 'inputs/missing-passes'))]=[]
    protector=sealed.Protector(service,w);protector.prepare(graph)
    try:protector.serve()
    except KeyboardInterrupt:pass


def wait(predicate):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.02)
    pytest.fail('isolated protector did not reach requested state')


@pytest.fixture
def snapshot(tmp_path,request):
    (tmp_path/'inputs/slices').mkdir(parents=True)
    (tmp_path/'_system/knowledge-builds/task-t/passes').mkdir(parents=True)
    (tmp_path/'inputs/slices/s.json').write_text('{}')
    (tmp_path/'inputs/task.json').write_text('{"value":1}')
    (tmp_path/'batch.json').write_text('{"task_ids":["t"]}')
    report=tmp_path/'_system/recovery/report.json';report.parent.mkdir(parents=True)
    report.write_text(json.dumps({'request':{'recovery_scope':'build-native/v1'},'entries':[
        {'node':'build-finalize','previous_task_id':'old','ended_run_ids':['1'],
         'output_seed':{'journal_name':'j','value':'first'}},
        {'node':'build-finalize','previous_task_id':'old','ended_run_ids':['2'],
         'output_seed':{'journal_name':'j','value':'second'}}]}))
    w={'workflow_id':'fixture','batch_id':'b','current_stage':'build_finalizing',
       'execution_recovery_history':[{'report_ref':'_system/recovery/report.json','report_sha256':hashlib.sha256(report.read_bytes()).hexdigest()}],
       'cancel_requested':False,'revision':1}
    workflow=tmp_path/'workflow.json';workflow.write_text(json.dumps(w))
    inherited_fd=os.open(report,os.O_RDWR)
    process=multiprocessing.get_context('fork').Process(target=serve,args=(tmp_path,w,getattr(request,'param',False)));process.start()
    directory=tmp_path/sealed.ROOT/'fixture'
    def active():
        try:return json.loads((directory/'state.json').read_text())['state']=='active'
        except (OSError,ValueError):return False
    try:
        wait(active)
        service=service_for(tmp_path);service._verified_json_reads=VerifiedJsonReads()
        service.status=lambda _:_load_json(workflow)
        yield NS(vault=tmp_path,w=w,service=service,directory=directory,process=process,report=report,workflow=workflow,fd=inherited_fd)
    finally:
        os.close(inherited_fd)
        if process.is_alive():
            process.terminate();process.join(5)
        # Fault-injection cleanup is confined to this isolated fixture. Restore
        # exactly the recorded flags, including after an intentional SIGKILL.
        if (directory/'manifest.json').exists():
            manifest=json.loads((directory/'manifest.json').read_text())
            for path in (directory,directory/'state.json',directory/'manifest.json',directory/'lease.lock'):
                if path.exists():sealed.flags(path,sealed.flags(path)&~sealed.IMMUTABLE)
            for path,bits in manifest['original_flags'].items():
                if Path(path).exists():sealed.flags(Path(path),bits)


def test_kernel_blocks_data_membership_symlink_and_privilege_bypass(snapshot,tmp_path):
    s=snapshot
    script=tmp_path/'consumer.py'
    script.write_text('''import json,os,sys
from pathlib import Path
from hermes_source_units.evidence_snapshot import restrict_consumer,flags,IMMUTABLE
p=Path(sys.argv[1]);d=Path(sys.argv[2]);restrict_consumer();restrict_consumer()
actions=[lambda:p.write_text('bad'),lambda:p.unlink(),lambda:p.rename(p.with_suffix('.moved')),lambda:os.write(int(sys.argv[3]),b'bad'),
 lambda:(d/'new.json').write_text('{}'),lambda:(d/'link.json').symlink_to(p),lambda:flags(p,flags(p)&~IMMUTABLE)]
blocked=0
for action in actions:
 try:action()
 except PermissionError:blocked+=1
assert blocked==len(actions),(blocked,len(actions))
assert 'NoNewPrivs:\\t1' in Path('/proc/self/status').read_text()
print(blocked)
''')
    env=dict(os.environ,PYTHONPATH=os.pathsep.join(sys.path))
    result=subprocess.run([sys.executable,str(script),str(s.report),str(s.vault/'inputs/slices'),str(s.fd)],env=env,pass_fds=(s.fd,),capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='7'


def test_report_queries_keep_order_duplicates_and_read_each_protected_report_once(snapshot):
    s=snapshot;a=NS(workflow=s.service);b={'workflow_id':'fixture','node':'build-finalize','task_id':'new'}
    with accounting_scope() as ledger,sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as view:
        for _ in range(8):
            assert [e['output_seed']['value'] for _,e in fixed_history.entries(a,b)]==['first','second']
            assert [e['value'] for _,e in fixed_history.journal_rows(a,b,'j','output_seed')]==['first','second']
            assert fixed_semantic_recovery.permitted_tasks(a,b,compact=True)['old']['ended_run_ids']==['2']
        rows=[r for r in ledger.rows.values() if r['path']==str(s.report)]
        assert sum(r['reads'] for r in rows)==1
        assert view.stats['first_reads']==1 and view.stats['hits']==23
        # The mutable workflow remains fresh at all 24 original query calls.
        assert sum(r['reads'] for r in ledger.rows.values() if r['path']==str(s.workflow))==24
        s.workflow.write_text(json.dumps({**s.w,'cancel_requested':True}))
        assert fixed_history.history_state(s.service,'fixture')['cancel_requested'] is True


def test_dependency_identity_matches_full_reads_and_repeat_has_no_static_bytes(snapshot,monkeypatch):
    s=snapshot
    from hermes_source_units import validation
    real=validation.validate_record
    monkeypatch.setattr(validation,'validate_record',lambda kind,value:None if kind=='knowledge_batch' else real(kind,value))
    with accounting_scope() as ledger,sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as view:
        first=dependencies(s.service,s.w)
        proof={'files':first['proof_files'],'directories':first['directories'],
            'schema_sha256':hashlib.sha256(Path(validation.__file__).with_name('schemas').joinpath('contracts.json').read_bytes()).hexdigest()}
        for _ in range(3):
            assert dependencies(s.service,s.w)['digest']==first['digest']
            recheck_dependencies(s.service,proof)
        count=view.stats['first_reads'];assert count==len(first['records'])-1
        with sealed.full_evidence_reads():
            full=dependencies(s.service,s.w)
            assert full['digest']==first['digest'] and full['proof_files']==first['proof_files']
        assert view.stats['first_reads']==count
        s.w['execution_recovery_history']=[]
        with pytest.raises(ContractError,match='binding changed'):dependencies(s.service,s.w)


def test_legal_writer_revokes_before_protected_mutation_and_audit_can_persist(snapshot):
    s=snapshot
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        original=read_bytes(s.report,'fixture')
        with pytest.raises(ContractError,match='requested revocation'):_write_atomic(s.report,b'{}')
        wait(lambda:json.loads((s.directory/'state.json').read_text())['state']=='revoked')
        assert s.report.read_bytes()==original
        with pytest.raises(ContractError):read_bytes(s.report,'fixture')
        with pytest.raises(ContractError):
            with sealed.full_evidence_reads():pytest.fail('revoked submission must not enter')
        _write_atomic(s.vault/'new-audit.json',b'{"raw_return":"preserved"}')
    with pytest.raises(ContractError):
        with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):pass


def test_revoke_waits_for_short_full_commit_and_not_for_model_lifetime(snapshot):
    s=snapshot;done=threading.Event();errors=[]
    def revoke():
        try:sealed.rpc(json.loads((s.directory/'state.json').read_text())['socket'],{'operation':'revoke'})
        except Exception as exc:errors.append(exc)
        done.set()
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        with sealed.full_evidence_reads():
            thread=threading.Thread(target=revoke);thread.start()
            assert not done.wait(.1)
            assert _load_json(s.report)['entries']
        assert done.wait(3) and not errors;thread.join()
        with pytest.raises(ContractError):read_bytes(s.report,'after-model-return')


def test_manager_crash_invalidates_memory_and_disk_active_marker(snapshot):
    s=snapshot
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        read_bytes(s.report,'fixture');os.kill(s.process.pid,signal.SIGKILL);s.process.join(5)
        with pytest.raises(ContractError):read_bytes(s.report,'fixture')


def test_same_mtime_privileged_replacement_is_detected_by_metadata(snapshot):
    s=snapshot
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        read_bytes(s.report,'fixture');stamp=s.report.stat().st_mtime_ns
        sealed.flags(s.report,sealed.flags(s.report)&~sealed.IMMUTABLE)
        s.report.write_text('{}');os.utime(s.report,ns=(stamp,stamp))
        sealed.flags(s.report,sealed.flags(s.report)|sealed.IMMUTABLE)
        with pytest.raises(ContractError,match='metadata changed'):read_bytes(s.report,'fixture')


def test_parent_symlink_replacement_is_detected_even_for_same_file(snapshot):
    s=snapshot
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        task=s.vault/'inputs/task.json';read_bytes(task,'fixture')
        original=s.vault/'inputs';moved=s.vault/'moved-inputs';original.rename(moved);original.symlink_to(moved,target_is_directory=True)
        try:
            with pytest.raises(ContractError):read_bytes(task,'fixture')
        finally:original.unlink();moved.rename(original)


def test_preparing_and_missing_registry_do_not_authorize_reuse(snapshot):
    s=snapshot
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False):
        read_bytes(s.report,'fixture')
        path=s.directory/'state.json';sealed.flags(s.directory,sealed.flags(s.directory)&~sealed.IMMUTABLE)
        sealed.flags(path,sealed.flags(path)&~sealed.IMMUTABLE)
        state=json.loads(path.read_text());path.write_text(json.dumps({**state,'state':'preparing'}))
        with pytest.raises(ContractError):read_bytes(s.report,'fixture')
        path.unlink()
        with pytest.raises(ContractError):read_bytes(s.report,'fixture')
        path.write_text(json.dumps(state))


@pytest.mark.parametrize('snapshot',[True,'deep'],indirect=True)
def test_absent_dependency_directory_uses_parent_seal_without_creating_data(snapshot):
    s=snapshot;manifest=json.loads((s.directory/'manifest.json').read_text())
    missing=Path(manifest['absent_directories'][0])
    assert not missing.exists()
    with sealed.snapshot_scope(s.service,directory=s.directory,restrict=False) as view:
        assert str(missing) in view.manifest['absent_directories']
        with view.guard((str(missing),)):pass
        with pytest.raises(PermissionError):missing.mkdir(parents=True)
        with pytest.raises(PermissionError):missing.symlink_to(s.report)
    assert not missing.exists()
