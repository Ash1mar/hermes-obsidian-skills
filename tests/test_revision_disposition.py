"""Isolated disposition/closure fixtures; never domain acceptance evidence."""
import copy,json,hashlib,sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_reduce_readiness import repair_fixture
from test_p3_knowledge_build import vault,workflow_request,ROOT
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from hermes_source_units.reduce_task_skip import skip,revision_slice_covered
from ingest_kanban import pass_node,domain_completed
from revision_disposition import verify


def test_partial_revision_preserves_outputs_and_needs_explicit_acceptance(vault,monkeypatch):
    a,w,card,_=repair_fixture(vault,monkeypatch);k=a.workflow.knowledge;b=k._batch(w['batch_id'])
    tid=next(t for t in b['task_ids'] if k._task(t)['target_refs'][0]['unit_ref']['resource_id']==card['node'].partition(':')[2])
    prior=max(k._pass_index([tid])[1][tid],key=lambda p:p['sequence'])
    ref='_system/reports/disposition-review.json';(vault/ref).write_text('Explicit isolated loss review')
    q=workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=w['revision'],revision_id='partial-fixture',
        revision_scope='reduce-dependencies/v1',evidence_digest='sha256:'+fingerprint(w['pause_control']),
        tasks=[{'task_id':tid,'pass_id':prior['pass_id'],'reason':'fixture dependency review'}],reduce_failures=[],
        reason='Isolated partial revision',evidence_refs=[ref],worker_template=(ROOT/'hermes-obsidian-governed-ingest-orchestrator/references/workers/pass-slice.md').read_text())
    monkeypatch.setattr('hermes_source_units.reduce_dependency_revision.citation_dependency_gaps',lambda *_:[{'missing':'fixture'}])
    a.revise_passes(q);w=a.workflow.status(w['workflow_id']);sid=w['pass_revision']['slice_ids'][0]
    item=k._slice(w['batch_id'],sid);item.update(state='cancelled',revision=item['revision']+1);k._write_slice(item)
    p={'workflow_id':w['workflow_id'],'actor':'agent','expected_revision':w['revision'],'revision_id':'partial-fixture',
       'operation_id':'dispose-fixture','reason':'Confirmed unverified fixture output; retain originals',
       'evidence_refs':[ref],'entries':[{'task_id':tid,'pass_id':prior['pass_id'],'reason':'Explicit coverage loss',
       'native_audit_ref':ref,'disposition_kind':'unverified_model_result'}]}
    from hermes_source_units import mutation_digest
    p['input_digest']=mutation_digest(p)
    proof=lambda *_:{'entries':{tid:{'native_audit_ref':ref,'packet_ref':ref}},'continuation_slice_ids':[],'uncalled_task_ids':[]}
    before={p:p.read_bytes() for p in vault.rglob('*.json') if '/passes/' in p.as_posix() or '/readings/' in p.as_posix() or '/resource-reductions/' in p.as_posix()}
    preview=skip(a.workflow,p,dry_run=True,revision_check=proof)
    assert preview['coverage_gaps'][0]['withheld_candidate_ids']==['claim']
    skip(a.workflow,p,revision_check=proof);live=a.workflow.status(w['workflow_id'])
    assert live['pass_revision']['tasks']==[] and live['state']=='cancelled'
    assert a.workflow.verify_pass_skips(live) and all(p.read_bytes()==v for p,v in before.items())
    node=pass_node(live,k._slice(w['batch_id'],sid));assert not domain_completed(a.workflow,live,node)
    done=a.workflow.finish_pass_revision(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=live['revision']))
    assert done['pass_revision']['state']=='awaiting_review' and not domain_completed(a.workflow,done,node)
    accepted=a.workflow.accept_pass_revision(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=done['revision'],
        revision_id='partial-fixture',evidence_digest=done['pass_revision']['review_evidence_digest'],evidence_refs=[ref]))
    assert domain_completed(a.workflow,accepted,node) and k._slice(w['batch_id'],sid)['state']=='cancelled'
    assert len(k._pass_index([tid])[1][tid])==2 and all(p.read_bytes()==v for p,v in before.items())


def native_fixture(tmp_path):
    wid='fixture';node='pass-slice:s';root=tmp_path/f'_system/ledgers/ingest-workflows/{wid}'
    for d in ('native-inputs','semantic-calls','reports'): (root/d).mkdir(parents=True)
    candidate={'sequence':1,'candidate_pass_id':'prior-a','revision_review':True,'candidates':[],'inspections':[]}
    task={'task':'t1','input_id':'i','next_sequence':2,'materials':[{'ref':'m1','role':'core','heading':0,'text_ref':'c1'}],
          'limits':{},'continuation':{'candidate':candidate}}
    packet={'texts':{'c1':'Actual authorized evidence.'},'headings':[['Scope']],'tasks':[task]}
    packet_path=root/'packet.json';packet_path.write_text(json.dumps(packet));packet_ref=packet_path.relative_to(tmp_path).as_posix()
    context={'owner':{'workflow_id':wid,'task_id':'native','node':node,'actor':'agent','template_hash':'fixed'},
             'packet_ref':packet_ref,'packet_sha256':hashlib.sha256(packet_path.read_bytes()).hexdigest(),'candidates':{'t1':'prior-a'}}
    (root/'native-inputs/fixture.json').write_text(json.dumps(context))
    exposed={k:copy.deepcopy(v) for k,v in task.items() if k not in ('input_id','next_sequence','continuation')}
    exposed['candidate']={k:v for k,v in candidate.items() if k!='candidate_pass_id'}
    view={**packet,'tasks':[exposed]};user='Semantic input v1\n'+json.dumps({'phase':'citation','input':view,'required_result_slots':['t1']})
    raw=json.dumps({'results':{'t1':{'blocked_reason':'Isolated unverified result'}}})
    home=tmp_path/'home';home.mkdir()
    with sqlite3.connect(home/'state.db') as db:
        db.executescript('CREATE TABLE sessions(id TEXT,source TEXT); CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
        db.execute('INSERT INTO sessions VALUES (?,?)',('native-session','kanban'))
        db.executemany('INSERT INTO messages VALUES (?,?,?,?)',[('native-session','user',user,None),('native-session','assistant',raw,None)])
    call={'phase':'citation','workflow_id':wid,'node':node,'native_task_id':'native','native_run_id':'1','template_hash':'fixed',
          'task_identities':{'t1':'a','t2':'b'},'user_sha256':fingerprint(user),'response_sha256':fingerprint(raw),
          'model':{'session_id':'native-session','origin':'hermes_native_model','response_sha256':fingerprint(raw)}}
    call_path=root/'semantic-calls/call.json';call_path.write_text(json.dumps(call));ref=call_path.relative_to(tmp_path).as_posix()
    failure={'task_id':'native','idempotency_key':'key','error_code':'SEMANTIC_DRAFT_REJECTED'}
    (root/f'reports/failed-{fingerprint(node)[:16]}.json').write_text(json.dumps(failure))
    value={'workflow_id':wid,'batch_id':'batch','actor':'agent','kanban':{'board_id':'board','task_map':[{'node':node,'task_id':'native','idempotency_key':'key'}]},
           'pass_revision':{'revision_id':'review','slice_ids':['s'],'tasks':[{'task_id':'a','pass_id':'prior-a','reading_package_id':'r'},
               {'task_id':'b','pass_id':'prior-b','reading_package_id':'r'}]}}
    for tid in ('a','b'):
        d=tmp_path/f'_system/knowledge-builds/task-{tid}/readings';d.mkdir(parents=True);(d/'r.json').write_text('{}')
    knowledge=SimpleNamespace(vault=tmp_path,model_input=lambda *_a,**_k:None,model_packet=lambda *_a,**_k:{'view':packet},
        _slice=lambda *_:{'task_ids':['a','b']},_pass_index=lambda tids:({}, {t:[] for t in tids}))
    snap={'task':{},'runs':[{'id':1,'worker_pid':123,'ended_at':2}]}
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,knowledge=knowledge),kanban=SimpleNamespace(task_snapshot=lambda *_:snap))
    request={'entries':[{'task_id':'a','native_audit_ref':ref,'disposition_kind':'unverified_model_result'}]}
    return adapter,value,request,home,call_path,snap


@pytest.mark.parametrize('case',['valid','uncalled','response','packet','active','permission','called_remainder'])
def test_native_disposition_binds_real_attempt_and_preserves_uncalled_work(tmp_path,case):
    a,w,q,home,path,snap=native_fixture(tmp_path)
    if case=='uncalled':q['entries'][0]['task_id']='b'
    if case=='response':
        with sqlite3.connect(home/'state.db') as db:db.execute("UPDATE messages SET content='changed' WHERE role='assistant'")
    if case=='packet':(path.parent.parent/'packet.json').write_text('changed')
    if case=='active':snap['task']['worker_pid']=123
    if case=='permission':q['entries'][0]['disposition_kind']='permission_failure'
    if case=='called_remainder':q['entries']=[]
    if case!='valid':
        with pytest.raises(ContractError):verify(a,w,q,home=home)
    else:
        proof=verify(a,w,q,home=home)
        assert proof['uncalled_task_ids']==['b'] and proof['continuation_slice_ids']==['s']
        assert set(proof['entries'])=={'a'}
