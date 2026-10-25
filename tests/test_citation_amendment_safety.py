"""Fast interruption, fencing and full-input equivalence checks."""
import copy
import hashlib
from types import SimpleNamespace
import pytest
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint
import hermes_source_units.citation_amendment as core
from citation_local_proof import page_input
import citation_amendment_calls as calls

def wallet(tmp_path):
    wid='isolated';base=core.root(tmp_path,wid)
    frozen={'selection':{'task_id':'original-task'},'budgets':{'citation':1,'body':1},
            'cumulative_limits':{'citation':2,'body':2}}
    path=base/'op/plan.json';_write_atomic(path,_json_bytes(frozen))
    value={'operation_id':'one','phase':'citation','plan_ref':path.relative_to(tmp_path).as_posix(),
           'plan_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    core.save(tmp_path,wid,value);_write_atomic(base/'budgets.json',_json_bytes({'jobs':{}}))
    return wid,value

@pytest.mark.parametrize('target_index',[0,1])
def test_half_written_transaction_fences_all_consumers_and_replays_once(tmp_path,monkeypatch,target_index):
    wid='interrupted';base=core.root(tmp_path,wid);one=tmp_path/'one.json';two=tmp_path/'two.json'
    _write_atomic(one,_json_bytes({'revision':0}));_write_atomic(two,_json_bytes({'revision':0}))
    writes=[core.row(tmp_path,one,{'revision':1}),core.row(tmp_path,two,{'revision':1})]
    actual=core._write_atomic;target=[one,two][target_index]
    def crash(path,data):
        actual(path,data)
        if path==target:raise RuntimeError('power loss after a control write')
    monkeypatch.setattr(core,'_write_atomic',crash)
    with pytest.raises(RuntimeError):core.transaction(tmp_path,wid,writes,'same-operation')
    with pytest.raises(ContractError,match='pending amendment'):core.state(tmp_path,wid)
    with pytest.raises(ContractError,match='exact pending'):core.transaction(tmp_path,wid,writes,'new-id')
    monkeypatch.setattr(core,'_write_atomic',actual);core.replay(tmp_path,wid)
    assert not (base/'pending.json').exists()
    assert one.read_bytes()==two.read_bytes()==_json_bytes({'revision':1})
    core.transaction(tmp_path,wid,writes,'same-operation')
    assert one.read_bytes()==two.read_bytes()

def test_unknown_reservation_survives_input_task_and_operation_changes(tmp_path):
    wid,value=wallet(tmp_path);binding={'workflow_id':wid,'node':'pass-slice:s','task_id':'native'}
    first=core.reserve(tmp_path,wid,'citation:original-task',{'user':'original'}, {},binding)
    second=core.reserve(tmp_path,wid,'citation:original-task',{'user':'original'}, {},{**binding,'task_id':'replacement'})
    assert second[-1] and len(second[1]['jobs']['citation:original-task']['attempts'])==1
    with pytest.raises(ContractError,match='input/version change'):
        core.reserve(tmp_path,wid,'citation:original-task',{'user':'different'}, {},binding)
    value['operation_id']='another';core.save(tmp_path,wid,value)
    # A different operation cannot make the reserved original disappear. Even
    # when expressly authorized, the second ordinal consumes the same lineage.
    core.reserve(tmp_path,wid,'citation:original-task',{'user':'original'}, {},binding)
    value['operation_id']='third';core.save(tmp_path,wid,value)
    with pytest.raises(ContractError,match='cumulative'):
        core.reserve(tmp_path,wid,'citation:original-task',{'user':'original'}, {},binding)
    assert len(core.pending_calls(tmp_path,wid)['jobs']['citation:original-task']['attempts'])==2

def test_old_and_build_workers_are_fenced_before_new_evidence_can_commit(tmp_path):
    wid,value=wallet(tmp_path)
    with pytest.raises(ContractError,match='fenced'):core.guard(tmp_path,wid,'global-reduce')
    with pytest.raises(ContractError,match='before Build'):core.guard(tmp_path,wid,'build-finalize:old')
    value['phase']='prepared';core.save(tmp_path,wid,value)
    with pytest.raises(ContractError,match='fenced'):core.guard(tmp_path,wid,'pass-slice:old')

def view(task='t1',handle='c1',text='x1',heading='1'):
    return {'identity':{'kind':'fact','identity_key':'same'},'path':'30_Cards/same.md','existing_page':None,
            'candidates':[{'candidate':handle,'finding':'same claim','conditions':[],
                           'evidence_qa':[{'qa':'usable','qa_note':''}],
                           'source_evidence':[{'reading':task,'support_refs':['m1']}]}],
            'original_evidence':{'tasks':[{'task':task,'materials':[{'ref':'m1','text_ref':text,'heading':int(heading),
                                     'asset':{'sha256':'a'*64},'role':'core'}]}],
                                 'texts':{text:'complete original text'},'headings':{heading:['Heading']}}}

@pytest.mark.parametrize('changed',['none','text','image','condition','qa','parent','pool'])
def test_reuse_normalizes_only_proven_aliases_and_checks_all_actual_writer_inputs(changed):
    old=view();new=view('t97','c800','x52','7')
    if changed=='text':new['original_evidence']['texts']['x52']='changed source'
    if changed=='image':new['original_evidence']['tasks'][0]['materials'][0]['asset']['sha256']='b'*64
    if changed=='condition':new['candidates'][0]['conditions']=['new restriction']
    if changed=='qa':new['candidates'][0]['evidence_qa'][0]['qa_note']='new shared limitation'
    if changed=='parent':new['existing_page']={'content':'different existing authored content'}
    if changed=='pool':
        new['original_evidence']['texts']['hidden']='unreferenced extra material'
        with pytest.raises(ContractError,match='unreferenced'):page_input(new)
    else:assert (page_input(old)==page_input(new))==(changed=='none')
