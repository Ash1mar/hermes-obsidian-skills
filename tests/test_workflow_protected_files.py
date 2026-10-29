"""Typed current-revision list segments; faults stay in isolated directories."""
import copy,hashlib,json,os,subprocess,sys
from pathlib import Path
import pytest
from test_workflow_storage import store
from hermes_source_units import workflow_storage as storage,evidence_snapshot as sealed,ContractError
from hermes_source_units.source_units import _load_json
from hermes_source_units.validation import fingerprint
from hermes_source_units.verified_json import VerifiedJsonReads
from hermes_source_units.read_accounting import accounting_scope

pytestmark=pytest.mark.skipif(os.name!='posix',reason='Linux immutable segment contract')


def publish(s,history=True):
    if history:s.service.migrate_storage(s.request('segments','history'))
    return s.service.migrate_storage(s.request('protected-files','protected'))


@pytest.mark.parametrize('history',[False,True])
def test_identity_duplicates_current_state_and_inline_recovery(store,history):
    s=store;publish(s,history)
    physical=json.loads(s.path.read_bytes());meta=physical[storage.MARKER]
    assert meta['contract']==storage.PROTECTED_CONTRACT
    assert physical['pass_revision']['protected_files']==[]
    assert meta[storage.PROTECTED]['revision_id']==s.state['pass_revision']['revision_id']
    assert _load_json(s.path)==s.state and fingerprint(_load_json(s.path))==fingerprint(s.state)
    assert len(_load_json(s.path)['pass_revision']['protected_files'])==2
    s.service.migrate_storage(s.request('inline','restore-inline'))
    assert storage.MARKER not in json.loads(s.path.read_bytes()) and _load_json(s.path)==s.state


def test_optional_history_field_stays_absent_and_history_can_be_segmented_later(store):
    s=store;s.state.pop('pass_revision_history');s.path.write_bytes(storage.compact(s.state))
    publish(s,False);assert _load_json(s.path)==s.state
    value=s.service._load(s.path.stem);value['revision']+=1;s.service._write(value)
    assert 'pass_revision_history' not in _load_json(s.path)
    value['pass_revision_history']=[{'state':'accepted','revision_id':'old'}];s.service._write(value)
    s.service.migrate_storage(s.request('segments','history-after-list'))
    assert _load_json(s.path)==value and len(storage.segment_paths(s.path,json.loads(s.path.read_bytes())))==2


@pytest.mark.parametrize('change',['new_revision','replace_list','remove_revision'])
def test_changed_revision_or_list_publishes_inline_without_mutating_old_object(store,change):
    s=store;publish(s);root=json.loads(s.path.read_bytes());target=storage.segment_paths(s.path,root)[-1][0]
    original=target.read_bytes();value=s.service._load(s.path.stem)
    if change=='new_revision':value['pass_revision'].update(revision_id='new',input_digest='sha256:'+'2'*64,state='running')
    elif change=='replace_list':value['pass_revision']['protected_files'].append({'ref':'source/new','sha256':'b'*64})
    else:value.pop('pass_revision')
    value['revision']+=1;s.service._write(value)
    assert _load_json(s.path)==value and target.read_bytes()==original
    physical=json.loads(s.path.read_bytes())
    assert storage.PROTECTED not in physical[storage.MARKER] and physical[storage.MARKER]['contract']==storage.CONTRACT
    if change=='new_revision':
        with pytest.raises(ContractError,match='accepted current revision'):
            s.service.migrate_storage(s.request('protected-files','too-early'))


def test_same_list_keeps_reference_but_state_and_cancel_are_always_fresh(store):
    s=store;publish(s);cache=VerifiedJsonReads();old=json.loads(s.path.read_bytes())
    first=cache.read(s.path);first['pass_revision']['protected_files'].clear()
    assert cache.read(s.path)==s.state
    value=s.service._load(s.path.stem);value['cancel_requested']=False
    value['pass_revision']['state']='awaiting_review';s.service._write(value)
    current=json.loads(s.path.read_bytes())
    assert current[storage.MARKER]==old[storage.MARKER]
    assert cache.read(s.path)==value and value['revision']==7
    # A direct same-mtime, no-revision control replacement is still observed.
    stamp=s.path.stat().st_mtime_ns;current['cancel_requested']=True
    s.path.write_bytes(storage.compact(current));os.utime(s.path,ns=(stamp,stamp))
    assert cache.read(s.path)['cancel_requested'] is True


@pytest.mark.parametrize('field',['revision_id','input_digest'])
def test_revision_binding_mismatch_fails_even_after_cache_hit(store,field):
    s=store;publish(s);cache=VerifiedJsonReads();cache.read(s.path)
    physical=json.loads(s.path.read_bytes());physical['pass_revision'][field]='changed'
    s.path.write_bytes(storage.compact(physical))
    with pytest.raises(ContractError,match='WORKFLOW_STORAGE_INVALID'):cache.read(s.path)


@pytest.mark.parametrize('fault',['replace','missing','symlink'])
def test_protected_list_cannot_be_hidden_by_cached_root(store,fault):
    s=store;publish(s);cache=VerifiedJsonReads();cache.read(s.path)
    target=storage.segment_paths(s.path,json.loads(s.path.read_bytes()))[-1][0];raw=target.read_bytes();stamp=target.stat().st_mtime_ns
    sealed.flags(target.parent,sealed.flags(target.parent)&~sealed.IMMUTABLE)
    sealed.flags(target,sealed.flags(target)&~sealed.IMMUTABLE)
    if fault=='replace':target.write_bytes(raw.replace(b'source/a',b'source/b'));os.utime(target,ns=(stamp,stamp))
    elif fault=='missing':target.unlink()
    else:target.unlink();target.symlink_to(s.path)
    try:
        with pytest.raises(ContractError,match='WORKFLOW_STORAGE_INVALID'):cache.read(s.path)
    finally:
        if target.is_symlink():target.unlink()
        if not target.exists():target.write_bytes(raw)


@pytest.mark.parametrize('after',[False,True])
def test_interrupted_list_publication_replays_without_changing_logical_identity(store,monkeypatch,after):
    s=store;s.service.migrate_storage(s.request('segments','history'));request=s.request('protected-files','protected')
    original=s.path.read_bytes();atomic=storage._atomic_root
    def crash(path,data):
        if after:atomic(path,data)
        raise OSError('isolated publication interruption')
    monkeypatch.setattr(storage,'_atomic_root',crash)
    with pytest.raises(OSError):s.service.migrate_storage(request)
    if not after:assert s.path.read_bytes()==original
    assert _load_json(s.path)==s.state
    monkeypatch.setattr(storage,'_atomic_root',atomic)
    assert s.service.migrate_storage(request)['ok'] and _load_json(s.path)==s.state


def test_kernel_protection_rejects_bypass_in_a_restricted_consumer(store):
    s=store;publish(s);target=storage.segment_paths(s.path,json.loads(s.path.read_bytes()))[-1][0]
    writable=s.path.parent/'unprotected-probe';writable.write_bytes(b'original')
    script='''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from hermes_source_units.evidence_snapshot import restrict_consumer,flags,IMMUTABLE
p=Path(sys.argv[2]);restrict_consumer();Path(sys.argv[3]).write_bytes(b'allowed')
blocked=[]
for action in (lambda:p.write_bytes(b'bad'),lambda:p.unlink(),lambda:flags(p,flags(p)&~IMMUTABLE),lambda:p.parent.rename(p.parent.with_name('renamed'))):
 try:action();blocked.append(False)
 except PermissionError:blocked.append(True)
print(json.dumps(blocked))
'''
    result=subprocess.run([sys.executable,'-c',script,str(Path(storage.__file__).parent.parent),str(target),str(writable)],check=True,capture_output=True,text=True)
    assert json.loads(result.stdout)==[True]*4 and writable.read_bytes()==b'allowed'


def test_each_root_hit_keeps_segment_guard_and_locked_full_reads(store):
    s=store;publish(s);batch=s.service.knowledge._batch_path('batch');raw=batch.read_bytes()
    protector=sealed.Protector(s.service,s.state)
    protector.prepare({'records':{batch:(raw,hashlib.sha256(raw).digest())},'directories':{},'bytes':len(raw)})
    protector.activate()
    try:
        with accounting_scope() as ledger,sealed.snapshot_scope(s.service,directory=protector.directory,restrict=False) as session:
            cache=VerifiedJsonReads()
            for _ in range(4):assert cache.read(s.path)==s.state
            assert session.stats['first_reads']==3 and session.stats['hits']==9 and session.stats['guards']==13
            rows=[r for r in ledger.rows.values() if r['origin']=='workflow_storage.protected_files']
            assert sum(r['reads'] for r in rows)==1
            with sealed.full_evidence_reads():assert cache.read(s.path)==s.state
            rows=[r for r in ledger.rows.values() if r['origin']=='workflow_storage.protected_files']
            assert sum(r['reads'] for r in rows)==2
            protector.revoke('isolated revocation')
            with pytest.raises(ContractError,match='EVIDENCE_SNAPSHOT_REVOKED'):cache.read(s.path)
    finally:
        if protector.state['state']!='revoked':protector.revoke('isolated cleanup')
