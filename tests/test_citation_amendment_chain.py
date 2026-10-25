"""Isolated native-protocol chain; these fixture facts never enter a real Vault."""
import copy
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import pytest
from test_p3_knowledge_build import vault,workflow_request
from test_reduce_execution_recovery import stopped_reducer,sync
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_guard import worker_binding,workflow_write_guard
from hermes_source_units.reduce_coverage import active,task_partition
from hermes_source_units.citation_amendment import state,root,plan,pending_calls
import fixed_reduce
import fixed_semantic as semantic
import citation_amendment_control as control
import citation_amendment_calls as calls

def fixture(vault,monkeypatch,mode='condition'):
    import test_reduce_execution_recovery as recovery_fixture
    original=recovery_fixture.prepare_layered_reduce_batch
    monkeypatch.setattr(recovery_fixture,'prepare_layered_reduce_batch',lambda v,b:original(v,b,initialize_slices=True,extra_candidate=True))
    a,w,card,*_=stopped_reducer(vault,monkeypatch,'global-reduce')
    k=a.workflow.knowledge;b=k._batch(w['batch_id']);base=vault/f'_system/ledgers/ingest-workflows/{w["workflow_id"]}'
    binding=json.loads(Path(a.kanban.by_id[card['task_id']]['body']['worker_binding']).read_text())
    old=fixed_reduce.preview_snapshot(a,binding)
    source_ref=f'{base.relative_to(vault).as_posix()}/semantic-inputs/old-scope.json'
    _write_atomic(vault/source_ref,_json_bytes(old))
    pages=[];rows=[];seals=[]
    for i,c in enumerate(old['view']['candidates']):
        p={'identity':{'kind':'fact','identity_key':f'fixture:{i}','canonical_name':f'Fixture {i}','aliases':[]},
           'path':f'30_Cards/fixture-{i}.md','candidate_refs':[c['candidate']]}
        raw=f'# Fixture {i}\n\nVerified fixture content.\n'.encode();ref=f'{base.relative_to(vault).as_posix()}/page-drafts/old-{i}.md'
        _write_atomic(vault/ref,raw);digest=hashlib.sha256(raw).hexdigest()
        audit_ref=f'{base.relative_to(vault).as_posix()}/semantic-calls/old-{i}.json'
        _write_atomic(vault/audit_ref,_json_bytes({'origin':'isolated previously admitted native fixture','response':raw.decode()}))
        journal=f'{base.relative_to(vault).as_posix()}/output-attempts/old-{i}.json'
        _write_atomic(vault/journal,_json_bytes({'contract':'finite-semantic-output/v1','phase':'page-write',
            'attempts':[{'state':'valid','attempt':1,'audit_ref':audit_ref}]}))
        seals.append({'ref':journal,'sha256':hashlib.sha256((vault/journal).read_bytes()).hexdigest(),
                      'audit_ref':audit_ref,'audit_semantic_sha256':fingerprint(json.loads((vault/audit_ref).read_text()))})
        row={**p,'draft_ref':ref,'draft_sha256':digest,'input_sha256':fingerprint(fixed_reduce.page_view(a,old,p)),
             'audit_ref':audit_ref,'storage_destination':p['path'],'submitted_content_sha256':digest,'authored_content_sha256':digest}
        pages.append(row)
        rows.append({'path':p['path'],'identity':p['identity'],
                     'candidate_refs':old['backend']['mapping'][c['candidate']],
                     'submitted_content_sha256':digest,'authored_content_sha256':digest})
    manifest={'workflow_id':w['workflow_id'],'allowed_pages':pages,'gaps':[],'allowed_page_count':len(pages),
              'held_page_count':0,'deferred_candidate_occurrences':0,'whole_batch_coverage_complete':False,
              'source_snapshot_ref':source_ref,'source_snapshot_sha256':hashlib.sha256((vault/source_ref).read_bytes()).hexdigest()}
    scope_dir=base/'coverage-scopes';manifest_ref=(scope_dir/'original.json').relative_to(vault).as_posix()
    _write_atomic(vault/manifest_ref,_json_bytes(manifest));manifest_sha=hashlib.sha256((vault/manifest_ref).read_bytes()).hexdigest()
    approval={'workflow_id':w['workflow_id'],'actor':w['actor'],'manifest_sha256':manifest_sha,'accept_necessary_coverage_gaps':True,
              'stop_after':'checkpoint_1','new_model_calls_granted':0}
    approval_ref=(scope_dir/'approval.json').relative_to(vault).as_posix();_write_atomic(vault/approval_ref,_json_bytes(approval))
    proof={'manifest_ref':manifest_ref,'manifest_sha256':manifest_sha,'approval_ref':approval_ref,
           'approval_sha256':hashlib.sha256((vault/approval_ref).read_bytes()).hexdigest(),'pages':rows,
           'new_model_calls_granted':0,'native_bodies_authenticated':len(rows),'native_planning_calls_authenticated':1,
           'finite_journal_seals':seals,'omitted_candidate_refs':[],**task_partition(k,old['backend']['tasks'],rows)}
    req=workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],recovery_id='prior-scope',recovery_scope='reduce-coverage/v1')
    report={'request':req,'batch_id':b['batch_id'],'entries':[{'node':'global-reduce','previous_task_id':'older-fixture',
        'coverage_admission':proof,'ended_run_ids':['1']}]}
    ref=(base/'execution-recoveries/prior.json').relative_to(vault).as_posix();_write_atomic(vault/ref,_json_bytes(report))
    w.setdefault('execution_recovery_history',[]).append({'recovery_id':'prior-scope','input_digest':req['input_digest'],
        'report_ref':ref,'report_sha256':hashlib.sha256((vault/ref).read_bytes()).hexdigest(),'nodes':['global-reduce']})
    a.workflow._write(w);scope=active(vault,w)
    run={'run_id':'original-scope','actor':w['actor'],'tasks':proof['run_tasks'],'expected_registry_revision':0,
         'document_registry_revision':1,'coverage_scope':scope['ref'],
         'decisions':[{**p,'action':'create','content':(vault/pages[i]['draft_ref']).read_text()}
                      for i,p in enumerate(rows)],'reason':'Already accepted isolated scope'}
    k.resume_cancelled_batch(b['batch_id'],w['actor'],k._batch(b['batch_id'])['revision'])
    with worker_binding(binding):
        assert k.reduce_global({'batch_id':b['batch_id'],'actor':w['actor'],'runs':[run],
                               'resource_reduction_ids':b['resource_reduction_ids'],'omitted_candidate_refs':[],
                               'coverage_scope':scope['ref']})['ok']
    k.cancel_batch(b['batch_id'],w['actor'],k._batch(b['batch_id'])['revision'])
    # Explicitly fixture an already reached checkpoint; new acceptance below
    # uses the real deterministic validator instead of a fabricated final result.
    w=a.workflow.status(w['workflow_id']);w.update(current_stage='checkpoint_1',resume_stage='checkpoint_1')
    _write_atomic(base/'old-pause.json',_json_bytes({'fixture':'prior accepted pause'}))
    _write_atomic(base/'old-decision.json',_json_bytes({'fixture':'prior accepted decision'}))
    w['pause_control']={'boundary':'checkpoint_1','evidence_digest':'sha256:'+'1'*64,
                       'report_ref':(base/'old-pause.json').relative_to(vault).as_posix(),'history':[]}
    w['checkpoints']['checkpoint_1']={'state':'approved','approval_digest':'sha256:'+'1'*64,'approved_by':'agent',
        'decision_ref':(base/'old-decision.json').relative_to(vault).as_posix(),
        'decision_sha256':hashlib.sha256((base/'old-decision.json').read_bytes()).hexdigest()}
    if not any(c['node']=='checkpoint-1-validate' for c in w['kanban']['task_map']):
        w['kanban']['task_map'].append({**card,'node':'checkpoint-1-validate'})
    a.workflow._write(w)
    selected_refs=next(refs for refs in old['backend']['mapping'].values() if refs[0]['candidate_id']=='pump-1')
    pid=selected_refs[0]['pass_id']
    tid=next(r['task_id'] for rid in b['resource_reduction_ids'] for r in k._resource_reduction(b['batch_id'],rid)['pass_refs'] if r['pass_id']==pid)
    prior=max(k._pass_index([tid])[1][tid],key=lambda p:p['sequence'])
    evidence=base/'confirmed-cause.json';_write_atomic(evidence,_json_bytes({'cause':'Isolated selected candidate condition requires correction'}))
    request=workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],
        recovery_id='bounded-one',recovery_scope='reduce-citation-revision/v1',action='prepare',
        target_contract_sha256=control.contract_hash(),reason='Correct only the selected fixture condition.',
        selections=[{'task_id':tid,'pass_id':pid,'candidate_ids':[prior['candidates'][0]['candidate_id']]}],
        basis=[{'ref':evidence.relative_to(vault).as_posix(),'sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}],
        budgets={'citation':1,'source':1,'plan':1,'body':2 if mode=='shared_qa' else 1},cumulative_limits={'citation':2,'source':2,'plan':2,'body':4})
    home=vault/'native-transcript-fixture';home.mkdir()
    with sqlite3.connect(home/'state.db') as db:
        db.executescript('CREATE TABLE sessions(id TEXT,source TEXT);CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);')
    real=__import__('fixed_semantic_recovery').transcript_reply
    monkeypatch.setattr('fixed_semantic_recovery.transcript_reply',lambda a,wire,**kw:real(a,wire,home=home))
    monkeypatch.setattr(calls,'verification_adapter',lambda _:a)
    return a,w,request,prior,old,pages,home

def command(a,request,action,**fields):
    w=a.workflow.status(request['workflow_id'])
    return workflow_request(workflow_id=w['workflow_id'],actor=w['actor'],expected_revision=w['revision'],
        recovery_id=request['recovery_id'],recovery_scope=request['recovery_scope'],
        target_contract_sha256=control.contract_hash(),action=action,**fields)

def finish_scoped_run(a,request,caller):
    wid=request['workflow_id'];current=a.workflow.status(wid)
    a.resume(workflow_request(workflow_id=wid,actor='agent',expected_revision=current['revision']))
    current=sync(a,wid);card=next(c for c in current['kanban']['task_map'] if c['node']=='global-reduce')
    gb=json.loads(Path(a.kanban.by_id[card['task_id']]['body']['worker_binding']).read_text())
    assert semantic.run_reduce(a,gb,caller)['ok'];a.kanban.ended.add(gb['task_id']);current=sync(a,wid)
    card=next(c for c in current['kanban']['task_map'] if c['node']=='checkpoint-1-validate')
    begun=a.worker_begin({'workflow_id':wid,'node':card['node'],'task_id':card['task_id']})
    assert a.worker_complete({'workflow_id':wid,'node':card['node'],'task_id':card['task_id'],
                              'template_hash':begun['template_hash']})['ok']
    a.kanban.ended.add(card['task_id']);current=sync(a,wid)
    assert a.workflow.pending_pause(current)=='checkpoint_1'
    assert a.recover_reduce_execution(command(a,request,'finish'))['phase']=='complete'
    return current

class NativeFixtureCaller:
    """Actual request/response hashes in an isolated Kanban-shaped transcript DB."""
    def __init__(self,home,mode='condition'):self.home=home;self.calls=[];self.mode=mode
    def __call__(self,phase,view,schema,correction):
        self.calls.append(phase)
        if phase=='citation':
            t=view['tasks'][0];c=copy.deepcopy(t['candidate']['candidates'][0]);c['conditions']=['Corrected bounded fixture condition']
            inspections=[]
            if self.mode=='shared_qa':
                inspection=copy.deepcopy(t['candidate']['inspections'][0]);inspection['qa_note']='New shared limitation verified in the original.'
                inspections=[inspection]
            draft={'results':{t['task']:{'decision':'revised','candidates':[c] if self.mode in ('condition','body_defer') else [],'removed_candidate_ids':[],
                                      'inspections':inspections,'empty_reason':'','review_note':'Checked original fixture material'}}}
        elif phase=='resource-reduce':
            draft={'proposals':[{'candidate_refs':[c['candidate'] for c in view['candidates']],
                                 'summary':'Corrected local fixture grouping','identity_hints':[],'path_hints':[]}],
                   'omitted_candidate_refs':[],'reason':'Native bounded source fixture'}
        elif phase=='global-detail':
            handles=[r[0] for r in view['candidates']]
            draft={'pages':[{'identity':p['identity'],'path':p['path'],
                             'candidate_refs':[h for h in p['candidate_refs'] if h in handles]}
                            for p in view['coordination']['planned_pages']],
                   'omitted_candidate_refs':[],'reason':'Retain exact fixture page scope'}
        elif phase=='page-write':draft=({'blocked_reason':'Fixture necessary condition still cannot be expressed safely'} if self.mode=='body_defer'
                                      else {'content':'# Corrected fixture\n\nCorrected bounded fixture condition.\n'})
        else:raise AssertionError(phase)
        raw=json.dumps(draft);wire=semantic.prepare_request_projection(phase,view,schema,[],correction)['wire']
        sid='isolated-'+str(len(self.calls));semantic.note_session(sid) if hasattr(semantic,'note_session') else __import__('semantic_output').note_session(sid)
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('INSERT INTO sessions VALUES (?,?)',(sid,'kanban'))
            db.executemany('INSERT INTO messages VALUES (?,?,?,?)',[(sid,'user',wire['user'],None),(sid,'assistant',raw,None)])
        return raw,{'origin':'hermes_native_model','session_id':sid,'response_sha256':fingerprint(raw)}

@pytest.mark.parametrize('mode',['condition','shared_qa','identity','body_defer'])
def test_bounded_native_successor_local_updates_reuse_and_new_checkpoint(vault,monkeypatch,tmp_path,mode):
    a,w,request,prior,old,pages,home=fixture(vault,monkeypatch,mode)
    original={(vault/p['draft_ref']): (vault/p['draft_ref']).read_bytes() for p in pages}
    assert a.recover_reduce_execution(request)['phase']=='prepared'
    assert a.recover_reduce_execution(command(a,request,'start'))['phase']=='citation'
    with pytest.raises(ContractError,match='fenced'):
        with worker_binding(old['binding']),workflow_write_guard(vault,kinds=('global-reduce',)):
            raise AssertionError('late old worker must never enter a domain mutation')
    current=a.workflow.status(w['workflow_id']);a.resume(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
    current=sync(a,w['workflow_id']);card=next(c for c in current['kanban']['task_map'] if c['node'].startswith('pass-slice:'))
    begun=a.worker_begin({'workflow_id':w['workflow_id'],'node':card['node'],'task_id':card['task_id'],'worker_id':'native-citation'})
    bound=begun['worker_request'];workspace=tmp_path/'native-worker';workspace.mkdir();_write_atomic(workspace/'worker-request.json',_json_bytes(bound))
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE',str(workspace));monkeypatch.setenv('HERMES_KANBAN_RUN_ID','1')
    packet=json.loads((vault/begun['model_input']['path']).read_text())
    from native_pass_identity import capture_context,verified_packet
    packet_path,packet=verified_packet(vault,begun['model_input'],a.worker_check(bound))
    capture_context(vault,bound,packet_path,packet)
    task=packet['tasks'][0];view=semantic.subset(packet,[task],'citation')
    caller=NativeFixtureCaller(home,mode)
    response,audit=semantic.model_call(a,bound,'citation',view,caller,30000,attempt_limit=1)
    authored,_,_=semantic.prepare_pass_items(a,response,packet,[task],'citation')
    item=authored[0];k=a.workflow.knowledge;mapping=k.model_input(prior['task_id'],json.loads((vault/f'_system/knowledge-builds/task-{prior["task_id"]}/readings/{prior["reading_package_id"]}.json').read_text()),persist=False)['mapping']
    def expand(ref):return copy.deepcopy(mapping[ref] if isinstance(ref,str) else ref)
    candidates=[{**c,'support_refs':[expand(r) for r in c['support_refs']]} for c in item['candidates']]
    inspections=[{**i,'source_ref':expand(i['source_ref'])} for i in item['inspections']]
    draft={k0:prior[k0] for k0 in ('task_id','actor','reading_package_id')}
    draft.update(expected_revision=k._task(prior['task_id'])['revision'],registry_revision=1,sequence=prior['sequence']+1,
                 pass_kind='citation',candidates=candidates,inspections=inspections,empty_reason='')
    with worker_binding(bound),workflow_write_guard(vault,kinds=('pass-slice',)):
        bad=copy.deepcopy(draft);bad['candidates'][1]['finding']='Unselected unauthorized mutation'
        rejected=k.record_pass_batch({'batch_id':w['batch_id'],'passes':[bad]})
        assert not rejected['ok'] and len(k._pass_index([prior['task_id']])[1][prior['task_id']])==2
        result=k.record_pass_batch({'batch_id':w['batch_id'],'passes':[draft]});assert result['ok'],result
    assert a.worker_complete(bound)['ok'];a.kanban.ended.add(bound['task_id']);current=sync(a,w['workflow_id'])
    assert current['pass_revision']['state']=='awaiting_review'
    assert a.recover_reduce_execution(command(a,request,'accept-successor'))['phase']=='downstream'
    # Resource/global native workers use the real fixed executor and new card identities.
    current=sync(a,w['workflow_id'])
    source_card=next(c for c in current['kanban']['task_map'] if c['node'] in state(vault,w['workflow_id'])['downstream_nodes'] and c['node'].startswith('resource-reduce:'))
    binding=json.loads(Path(a.kanban.by_id[source_card['task_id']]['body']['worker_binding']).read_text())
    assert semantic.run_reduce(a,binding,caller)['ok'];a.kanban.ended.add(binding['task_id']);current=sync(a,w['workflow_id'])
    global_card=next(c for c in current['kanban']['task_map'] if c['node']=='global-reduce')
    gb=json.loads(Path(a.kanban.by_id[global_card['task_id']]['body']['worker_binding']).read_text())
    with pytest.raises(ContractError,match='Local native revision finished'):semantic.run_reduce(a,gb,caller)
    a.kanban.ended.add(gb['task_id'])
    # Record the supported stop on its original binding, then drain it.
    a.execution_failure(gb,'WORKFLOW_STOPPED','Local native revision finished')
    current=a.workflow.status(w['workflow_id']);a.cancel(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
    amendment=state(vault,w['workflow_id']);work=json.loads((vault/amendment['work_ref']).read_text())
    assert amendment['manifest_sha256']!=plan(vault,amendment)['scope']['ref']['manifest_sha256']
    expected=['citation']+([] if mode=='identity' else ['resource-reduce','global-detail']+['page-write']*(2 if mode=='shared_qa' else 1))
    assert len(work['reuse_proofs'])==len(pages)-(0 if mode=='identity' else 2 if mode=='shared_qa' else 1)
    assert len(work['held'])==int(mode=='body_defer')
    assert caller.calls==expected
    assert all(p.read_bytes()==raw for p,raw in original.items())
    approval={'workflow_id':w['workflow_id'],'actor':'agent','manifest_sha256':amendment['manifest_sha256'],
              'accept_necessary_coverage_gaps':True,'stop_after':'checkpoint_1','new_model_calls_granted':0}
    approval_path=root(vault,w['workflow_id'])/'approval.json';_write_atomic(approval_path,_json_bytes(approval))
    admission=command(a,request,'admit',approval_ref=approval_path.relative_to(vault).as_posix(),
                      approval_sha256=hashlib.sha256(approval_path.read_bytes()).hexdigest())
    work_path=vault/amendment['work_ref'];sealed_work=work_path.read_bytes()
    work_path.write_bytes(sealed_work+b' ')
    with pytest.raises(ContractError,match='pinned coverage evidence changed'):
        a.recover_reduce_execution(admission)
    work_path.write_bytes(sealed_work)
    assert a.recover_reduce_execution(admission)['ok']
    current=finish_scoped_run(a,request,caller)
    assert caller.calls==expected
    assert not any(p['boundary']=='checkpoint_1' for p in current['pause_control']['history'])
    if mode=='body_defer':
        a.cancel(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
        review=root(vault,w['workflow_id'])/'new-body-cause.json'
        _write_atomic(review,_json_bytes({'corrected_cause':'Prior refusal expanded unrelated package obligations; assigned evidence is complete.'}))
        basis=[{'ref':review.relative_to(vault).as_posix(),'sha256':hashlib.sha256(review.read_bytes()).hexdigest()}]
        restore=command(a,request,'reopen-downstream',target_paths=[work['held'][0]['path']],basis=basis,
                        clarification='Check only the assigned candidate and its actual necessary conditions; other package topics are not writing obligations.')
        assert a.recover_reduce_execution(restore)['citation_and_source_reused']
        logical=work['held'][0]['logical_work'];attempt=pending_calls(vault,w['workflow_id'])['jobs'][logical]['attempts'][-1]
        assert a.recover_reduce_execution(command(a,request,'authorize-call',logical_work=logical,
            supersedes_attempt_sha256=fingerprint(attempt),basis=basis,reason='Corrected the explicit scope misunderstanding.'))['additional_calls']==1
        caller.mode='condition'
        current=a.workflow.status(w['workflow_id']);a.resume(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
        current=sync(a,w['workflow_id']);card=next(c for c in current['kanban']['task_map'] if c['node']=='global-reduce')
        binding=json.loads(Path(a.kanban.by_id[card['task_id']]['body']['worker_binding']).read_text())
        with pytest.raises(ContractError,match='Local native revision finished'):semantic.run_reduce(a,binding,caller)
        a.kanban.ended.add(binding['task_id']);a.execution_failure(binding,'WORKFLOW_STOPPED','Held-body recovery finished')
        current=a.workflow.status(w['workflow_id']);a.cancel(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
        updated=state(vault,w['workflow_id']);new_manifest=json.loads((vault/updated['manifest_ref']).read_text())
        assert not new_manifest['gaps'] and caller.calls==expected+['page-write']
        approval.update(manifest_sha256=updated['manifest_sha256']);_write_atomic(approval_path,_json_bytes(approval))
        assert a.recover_reduce_execution(command(a,request,'admit',approval_ref=approval_path.relative_to(vault).as_posix(),
            approval_sha256=hashlib.sha256(approval_path.read_bytes()).hexdigest()))['ok']
        current=finish_scoped_run(a,request,caller)
        assert caller.calls==expected+['page-write']
        assert len(pending_calls(vault,w['workflow_id'])['jobs'][logical]['attempts'])==2
