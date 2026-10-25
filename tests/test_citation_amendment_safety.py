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

def test_reserved_unknown_is_not_resent_after_worker_replacement(tmp_path,monkeypatch):
    wid,_=wallet(tmp_path);monkeypatch.setattr(calls,'native_home',lambda:tmp_path/'absent-native-home')
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path),worker_check=lambda _:None)
    binding={'workflow_id':wid,'node':'pass-slice:s','task_id':'old','template_hash':'a'*64}
    view={'tasks':[{'task':'t1','materials':[],'candidate':{'revision_review':True,'sequence':1,
          'candidates':[],'inspections':[],'empty_reason':''}}],'texts':{},'headings':[]}
    sent=[]
    def unknown():
        sent.append(1);calls.note_session('unresolved')
        raise ContractError('NATIVE_COMPLETION_UNKNOWN','$','stream ended without a final response')
    with pytest.raises(ContractError,match='NATIVE_COMPLETION_UNKNOWN'):
        calls.invoke(adapter,binding,'citation',view,None,30000,None,unknown)
    with pytest.raises(ContractError,match='do not resend'):
        calls.invoke(adapter,{**binding,'task_id':'replacement'},'citation',view,None,30000,None,unknown)
    assert len(sent)==1
    record=core.pending_calls(tmp_path,wid)['jobs']['citation:original-task']['attempts'][0]
    assert record['state']=='reserved' and record['session_id']=='unresolved'

def test_stale_worker_cannot_consume_a_new_reservation(tmp_path):
    wid,_=wallet(tmp_path)
    def denied(_):raise ContractError('ACCESS_DENIED','$','old native card')
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path),worker_check=denied)
    binding={'workflow_id':wid,'node':'pass-slice:s','task_id':'old','template_hash':'a'*64}
    with pytest.raises(ContractError,match='old native card'):
        calls.invoke(adapter,binding,'citation',{'tasks':[{'task':'t1'}]},None,30000,None,
                     lambda:(_ for _ in ()).throw(AssertionError('must never dispatch')))
    assert core.pending_calls(tmp_path,wid)['jobs']=={}

def test_native_card_must_belong_to_the_same_workflow_and_vault(tmp_path):
    import json
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,status=lambda _:{'kanban':{'board_id':'isolated'}}),
        kanban=SimpleNamespace(task_snapshot=lambda *_:{'task':{'created_by':'ingest-workflow',
            'body':json.dumps({'workflow_id':'foreign','node':'pass-slice:s','vault':'/another-vault'})},'runs':[]}))
    binding={'workflow_id':'isolated','node':'pass-slice:s','task_id':'old','template_hash':'a'*64}
    with pytest.raises(ContractError,match='owner, node or Vault'):
        calls.proof(adapter,binding,{'binding':binding,'audit_ref':'unread.json'})

def test_saved_native_session_reconciles_after_crash_without_a_second_request(tmp_path,monkeypatch):
    import json,sqlite3
    import fixed_semantic as semantic
    import fixed_semantic_recovery as recovery
    from fixed_semantic_schema import slot_schema
    wid,_=wallet(tmp_path);home=tmp_path/'native';home.mkdir()
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID','1');monkeypatch.setattr(calls,'native_home',lambda:home)
    real=recovery.transcript_reply
    monkeypatch.setattr(recovery,'transcript_reply',lambda a,wire,**kw:real(a,wire,home=home))
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,status=lambda _:{'kanban':{'board_id':'isolated'}}),
        kanban=SimpleNamespace(task_snapshot=lambda *_:{'task':{'created_by':'ingest-workflow',
            'body':json.dumps({'workflow_id':wid,'node':'pass-slice:s','vault':str(tmp_path)})},
            'runs':[{'id':1,'ended_at':2}]}),worker_check=lambda _:None)
    binding={'workflow_id':wid,'node':'pass-slice:s','task_id':'old','template_hash':'a'*64}
    view={'tasks':[{'task':'t1','materials':[],'candidate':{'revision_review':True,'sequence':1,
          'candidates':[],'inspections':[],'empty_reason':''}}],'texts':{},'headings':[]}
    schema=slot_schema('citation',view['tasks']);wire=semantic.prepare_request_projection('citation',view,schema,[])['wire']
    raw=json.dumps({'results':{'t1':{'decision':'revised','candidates':[],'removed_candidate_ids':[],
                                  'inspections':[],'empty_reason':'','review_note':'Isolated protocol response'}}})
    sent=[]
    def completed_then_crashed():
        sent.append(1);calls.note_session('persisted-native')
        with sqlite3.connect(home/'state.db') as db:
            db.executescript('CREATE TABLE sessions(id TEXT,source TEXT);CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
            db.execute('INSERT INTO sessions VALUES (?,?)',('persisted-native','kanban'))
            db.executemany('INSERT INTO messages VALUES (?,?,?,?)',[('persisted-native','user',wire['user'],None),('persisted-native','assistant',raw,None)])
        raise RuntimeError('power loss before audit persistence')
    with pytest.raises(RuntimeError):calls.invoke(adapter,binding,'citation',view,None,30000,None,completed_then_crashed)
    result,ref=calls.invoke(adapter,{**binding,'task_id':'replacement'},'citation',view,None,30000,None,completed_then_crashed)
    assert len(sent)==1 and result['reviews'][0]['decision']=='revised'
    assert ref.startswith('_system/ledgers/ingest-workflows/isolated/semantic-calls/reconciled-')
    record=core.pending_calls(tmp_path,wid)['jobs']['citation:original-task']['attempts'][0]
    assert record['reconciliation']['prior_state']=='reserved' and record['state']=='received'

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
