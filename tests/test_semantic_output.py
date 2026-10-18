"""Persistent bounded correction contracts; isolated fixtures, no native acceptance."""
import copy,json
from types import SimpleNamespace
import pytest
import semantic_output as output
import fixed_semantic as fixed
import fixed_semantic_recovery as recovery
import fixed_global_packets as g
from hermes_source_units import ContractError
from test_global_plan_inputs import fixture_value


def fixture(tmp_path,monkeypatch,answers):
    value=fixture_value();state=g.initial();packet=g.next_packet(value,g.units(value),state,fixed.prepare_request_projection)
    binding={'workflow_id':'fixture','node':'global-reduce','task_id':'native-one','template_hash':'fixture'}
    adapter=SimpleNamespace(workflow=SimpleNamespace(vault=tmp_path,status=lambda _: {'execution_recovery_history':[]}),worker_check=lambda _:None)
    calls=[];saved={}
    def call(a,b,p,v,c,limit,correction=None,**kwargs):
        calls.append(copy.deepcopy(correction));answer=answers[len(calls)-1]
        if isinstance(answer,Exception):output.note_session('uncertain-native');raise answer
        ref='audit-'+str(len(calls));saved[ref]=copy.deepcopy(answer);output.note_audit(ref)
        return copy.deepcopy(answer),ref
    monkeypatch.setattr(fixed,'attach_receipt',lambda *args:None)
    monkeypatch.setattr(recovery,'native_reply',lambda a,b,path,*args: (json.dumps(saved[path.name]),{}) if path.name in saved else None)
    def run():return output.execute(adapter,binding,'global-plan',packet['view'],object(),call,
        lambda d:output.check('global-plan',value,packet,state,d),packet=packet)
    return SimpleNamespace(run=run,calls=calls,saved=saved,binding=binding,packet=packet,adapter=adapter)


def answer(bad=False):
    return {'pages':[{'identity':{'kind':'requirement','identity_key':'v'+str(n),'canonical_name':'V'+str(n),'aliases':[]},
        'path':'30_Cards/v'+str(n)+'.md','proposal_refs':['p'+str(n)]} for n in (1,2)],
        'detail_unit_refs':['u1'] if bad else [],'reason':'Isolated routing fixture'}


def test_valid_output_adds_no_call_and_is_reused(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer()]);assert c.run()[0]==answer();assert c.run()[0]==answer();assert c.calls==[None]


def stopped_output(c):
    import hashlib
    path=output.path_for(c.adapter,c.binding,'global-plan',c.packet['view']);record=json.loads(path.read_text())
    for attempt in record['attempts']:
        ref=attempt['audit_ref'];audit_path=c.adapter.workflow.vault/ref
        actual=path.parent.parent/'semantic-calls'/audit_path.name;actual.parent.mkdir(exist_ok=True)
        actual.write_text(json.dumps({'workflow_id':'fixture','node':'global-reduce','phase':'global-plan',
            'native_task_id':attempt['task_id'],'input_sha256':attempt['wire_sha256'],
            'schema_sha256':record['schema_sha256']}))
        c.saved[actual.name]=c.saved[ref];attempt['audit_ref']=actual.relative_to(c.adapter.workflow.vault).as_posix()
    path.write_text(json.dumps(record))
    selected={'attempt_ref':path.relative_to(c.adapter.workflow.vault).as_posix(),
        'attempt_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    return path,selected


def test_interrupted_first_rejection_keeps_unused_second_then_stops(tmp_path,monkeypatch):
    import hashlib
    c=fixture(tmp_path,monkeypatch,[answer(True),answer()]);normal=c.adapter.worker_check;checks=0
    def interrupted(_):
        nonlocal checks
        checks+=1
        if checks==3:raise ContractError('WORKFLOW_STOPPED','$','isolated stop before correction')
    c.adapter.worker_check=interrupted
    with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):c.run()
    path,selected=stopped_output(c);original=json.loads(path.read_text())['attempts'][0]
    status=output.interruption_status(c.adapter,'fixture')['outputs'][0]
    assert status['next_action']=='use_remaining_correction' and status['remaining_standard_calls']==1
    boundary=output.verify_stop_boundary(c.adapter,'fixture','global-reduce',{'stop_after_output':selected})
    report=tmp_path/'cancel-recovery.json';report.write_text(json.dumps({'request':{'recovery_scope':'reduce-cancel/v1'},'entries':[{'node':'global-reduce',**boundary}]}))
    pointer={'report_ref':report.name,'report_sha256':hashlib.sha256(report.read_bytes()).hexdigest(),'recovery_id':'bounded-resume'}
    c.adapter.workflow.status=lambda _:{'execution_recovery_history':[pointer]};c.adapter.worker_check=normal
    assert c.run()[0]==answer() and len(c.calls)==2
    assert json.loads(path.read_text())['attempts'][0]==original
    with pytest.raises(ContractError,match='requested output boundary reached'):
        output.stop_at_output_boundary(c.adapter,c.binding,'global-plan',c.packet['view'])
    other=copy.deepcopy(c.packet['view']);other['isolated_other_packet']=True
    with pytest.raises(ContractError,match='only its selected output call'):
        output.execute(c.adapter,c.binding,'global-plan',other,object(),lambda *a,**kw:pytest.fail('out-of-scope model call'),lambda d:d)
    assert len(c.calls)==2
    marker=tmp_path/'_system/ledgers/ingest-workflows/fixture/reports/output-boundary-bounded-resume.json'
    assert json.loads(marker.read_text())['further_calls_authorized'] is False
    # Resuming the same limit cannot dispatch a third call, even after a crash.
    assert c.run()[0]==answer() and len(c.calls)==2
    with pytest.raises(ContractError,match='requested output boundary reached'):
        output.stop_at_output_boundary(c.adapter,c.binding,'global-plan',c.packet['view'])
    c.adapter.workflow.status=lambda _:{'execution_recovery_history':[pointer,{
        'report_ref':'next.json','report_sha256':hashlib.sha256(b'{"entries":[{"node":"global-reduce"}]}').hexdigest()}]}
    (tmp_path/'next.json').write_bytes(b'{"entries":[{"node":"global-reduce"}]}')
    output.stop_at_output_boundary(c.adapter,c.binding,'global-plan',c.packet['view'])


@pytest.mark.parametrize('change',['hash','node','exhausted','uncertain'])
def test_interruption_limit_never_supplies_retry_or_acceptance_authority(tmp_path,monkeypatch,change):
    import hashlib
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True)])
    with pytest.raises(ContractError):c.run()
    path,selected=stopped_output(c);record=json.loads(path.read_text())
    if change=='node':node='unrelated'
    else:node='global-reduce'
    if change=='uncertain':record['attempts'][-1]['state']='reserved';path.write_text(json.dumps(record))
    if change!='hash':selected['attempt_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    else:selected['attempt_sha256']='0'*64
    with pytest.raises(ContractError):output.verify_stop_boundary(c.adapter,'fixture',node,{'stop_after_output':selected})
    assert len(c.calls)==2


def test_one_precise_correction_then_restart_reuses_success(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer()]);c.run()
    assert len(c.calls)==2 and c.calls[1]['assigned_and_requested']==['p1']
    c.binding['task_id']='fresh-after-cancel';assert c.run()[0]==answer();assert len(c.calls)==2


def test_two_failures_and_prompt_change_do_not_reset_allowance(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True)])
    for _ in range(2):
        with pytest.raises(ContractError,match='DUPLICATE'):c.run()
    assert len(c.calls)==2
    monkeypatch.setitem(fixed.RULES,'global-plan',fixed.RULES['global-plan']+' Changed prompt.')
    with pytest.raises(ContractError,match='allowance is not reset'):c.run()
    assert len(c.calls)==2


def test_correction_interrupted_after_audit_is_revalidated_without_resending(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer()]);normal=output.write;interrupted=False
    def write(path,record):
        nonlocal interrupted
        normal(path,record)
        if len(record['attempts'])==2 and record['attempts'][1].get('audit_ref') and not interrupted:
            interrupted=True;raise RuntimeError('isolated crash after audit link')
    monkeypatch.setattr(output,'write',write)
    with pytest.raises(RuntimeError):c.run()
    assert c.run()[0]==answer() and len(c.calls)==2


def test_uncertain_transport_is_not_an_output_correction(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[RuntimeError('connection failure')])
    with pytest.raises(RuntimeError):c.run()
    with pytest.raises(ContractError,match='completion is uncertain'):c.run()
    assert len(c.calls)==1


def test_material_refusal_and_invalid_identity_never_get_correction(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[{'blocked_reason':'Required evidence is insufficient'}])
    with pytest.raises(ContractError,match='SEMANTIC_REVIEW_REQUIRED'):c.run()
    assert c.calls==[None]
    c.adapter.worker_check=lambda _:(_ for _ in ()).throw(ContractError('STALE_INPUT','$','changed hash'))
    with pytest.raises(ContractError,match='STALE_INPUT'):c.run()
    assert c.calls==[None]


def test_raw_native_verification_does_not_accept_changed_authority(tmp_path):
    from test_reduce_semantic_recovery import call_fixture
    c=call_fixture(tmp_path,'global-plan')
    assert recovery.native_reply(c.adapter,c.binding,c.audit_path,c.wire,c.output_schema,[],home=c.home)[0]
    c.audit['native_task_id']='unrelated';c.audit_path.write_text(json.dumps(c.audit))
    assert recovery.native_reply(c.adapter,c.binding,c.audit_path,c.wire,c.output_schema,[],home=c.home) is None


def test_real_model_call_admits_only_the_reserved_correction(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[]);calls=[];value=fixture_value()
    def caller(phase,view,output_schema,correction=None):
        calls.append(correction)
        return json.dumps(answer(not bool(correction))),{'origin':'isolated_fixture'}
    draft,_,_=output.execute(c.adapter,c.binding,'global-plan',c.packet['view'],caller,fixed.model_call,
        lambda d:output.check('global-plan',value,c.packet,g.initial(),d),packet=c.packet)
    assert draft==answer() and len(calls)==2 and calls[1]['assigned_and_requested']==['p1']


def test_proven_uncalled_reservation_continues_same_second_ordinal(tmp_path,monkeypatch):
    import hashlib
    c=fixture(tmp_path,monkeypatch,[]);value=fixture_value();calls=[];old_guard=True
    def caller(phase,view,output_schema,correction=None):
        calls.append(correction);return json.dumps(answer(not bool(correction))),{'origin':'isolated_fixture'}
    def invoke(*args,**kwargs):
        if old_guard:kwargs['attempt_limit']=1  # reproduce the pre-SDK admission defect
        return fixed.model_call(*args,**kwargs)
    def run():return output.execute(c.adapter,c.binding,'global-plan',c.packet['view'],caller,invoke,
        lambda d:output.check('global-plan',value,c.packet,g.initial(),d),packet=c.packet)
    with pytest.raises(ContractError,match='INVALID_TRANSITION'):run()
    path=output.path_for(c.adapter,c.binding,'global-plan',c.packet['view']);record=json.loads(path.read_text());last=record['attempts'][1]
    assert len(calls)==1 and not last.get('audit_ref') and not last.get('session_id')
    original=record['attempts'][0]['audit_ref']
    monkeypatch.setattr(recovery,'native_reply',lambda *args,**kwargs:(json.dumps(answer(True)),{}))
    report=tmp_path/'grant.json';report.write_text(json.dumps({'request':{'recovery_scope':output.SCOPE},'entries':[{'node':'global-reduce',
        'uncalled_reservation':{'journal_name':path.name,'attempt_id':last['attempt_id'],'task_id':last['task_id']}}]}))
    pointer={'report_ref':'grant.json','report_sha256':hashlib.sha256(report.read_bytes()).hexdigest()}
    c.adapter.workflow.status=lambda _: {'execution_recovery_history':[pointer]};c.binding['task_id']='new-after-ended-attempt';old_guard=False
    assert run()[0]==answer() and len(calls)==2
    record=json.loads(path.read_text());assert len(record['attempts'])==2 and len(record['attempts'][1]['not_called_history'])==1


def test_admission_and_canonical_replay_share_exact_recovery_identity():
    from hermes_source_units.execution_plan import execution_recovery_key as key
    a={'node':'global-reduce','previous_task_id':'ended-one'};b={**a,'previous_task_id':'ended-two'}
    assert key('semantic-output/v1',a)!=key('semantic-output/v1',b)
    assert key('semantic-output/v1',a)==key('semantic-output/v1',dict(a))
    assert key('source-format/v1',a)==key('source-format/v1',b)


def test_diagnosis_collects_path_identity_and_coverage_findings_without_patching():
    target=answer()['pages'][0];draft=answer();draft['pages'][1]['path']=target['path']
    draft['pages'][0]['identity']['aliases']=['changed alias'];draft['pages'][1]['proposal_refs']=[]
    original=copy.deepcopy(draft)
    packet={'proposal_refs':['p1','p2'],'units':[],
        'view':{'coordination':{'planned_pages':[target]}}}
    d=output.diagnosis('global-plan',packet,draft,ContractError('INCOMPLETE_COVERAGE','$','coverage'))
    assert d['missing']==['p2'] and d['failed_response']==original and draft==original
    assert {f['rule'] for f in d['page_findings']}=={'distinct_identities_share_path','append_requires_exact_offered_identity_and_path'}
    assert all(d['offered_targets'][f['offered_target_ref']]['identity']==target['identity'] for f in d['page_findings'] if 'offered_target_ref' in f)


def test_diagnostic_upgrade_keeps_old_successful_correction_wire(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer()]);c.run()
    old=json.loads(output.path_for(c.adapter,c.binding,'global-plan',c.packet['view']).read_text())
    monkeypatch.setattr(output,'diagnosis',lambda *args: {'different':'new diagnostic rules'})
    assert c.run()[0]==answer() and len(c.calls)==2
    assert json.loads(output.path_for(c.adapter,c.binding,'global-plan',c.packet['view']).read_text())==old


def grant_continuation(c):
    import hashlib
    from hermes_source_units.validation import fingerprint
    path=output.path_for(c.adapter,c.binding,'global-plan',c.packet['view']);record=json.loads(path.read_text())
    permit={'journal_name':path.name,'prior_attempts_sha256':fingerprint(record['attempts']),
        'input_sha256':record['input_sha256'],'schema_sha256':record['schema_sha256'],'attempt_ordinal':3,
        'diagnostic':{'reviewed':'precise mechanical findings','failed_response':answer(True)}}
    report=path.parent.parent/'explicit-grant.json';report.write_text(json.dumps({'request':{'recovery_scope':output.SCOPE},
        'entries':[{'node':c.binding['node'],'exhausted_continuation':permit}]}))
    ref=report.relative_to(c.adapter.workflow.vault).as_posix()
    c.adapter.workflow.status=lambda _: {'execution_recovery_history':[{'report_ref':ref,'report_sha256':hashlib.sha256(report.read_bytes()).hexdigest()}]}
    return path


@pytest.mark.parametrize('success',[True,False])
def test_explicit_continuation_retains_three_total_calls_and_never_gets_fourth(tmp_path,monkeypatch,success):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True),answer(not success)])
    with pytest.raises(ContractError):c.run()
    path=grant_continuation(c);c.binding['task_id']='explicitly-authorized-ended-successor'
    for _ in range(2):
        if success:assert c.run()[0]==answer()
        else:
            with pytest.raises(ContractError):c.run()
    r=json.loads(path.read_text());assert len(c.calls)==3 and [a['attempt'] for a in r['attempts']]==[1,2,3]
    assert r['attempts'][2]['authorization_ref'] and r['attempts'][2]['state']==('valid' if success else 'rejected')


def test_changed_prior_attempts_cannot_use_explicit_continuation(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True)])
    with pytest.raises(ContractError):c.run()
    path=grant_continuation(c);record=json.loads(path.read_text());record['attempts'][0]['wire_sha256']='changed'
    path.write_text(json.dumps(record))
    with pytest.raises(ContractError,match='exact prior attempts'):c.run()
    assert len(c.calls)==2


def test_diagnostic_size_failure_preserves_first_call_and_uses_only_unused_second_slot(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer()]);normal=output.diagnosis
    monkeypatch.setattr(output,'diagnosis',lambda *args,**kwargs:{**normal(*args,**kwargs),'isolated_repeated_diagnostic':'x'*40000})
    with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):c.run()
    path=output.path_for(c.adapter,c.binding,'global-plan',c.packet['view']);old=json.loads(path.read_text())['attempts'][0]
    assert len(c.calls)==1
    monkeypatch.setattr(output,'diagnosis',normal)
    assert c.run()[0]==answer() and len(c.calls)==2
    record=json.loads(path.read_text());assert record['attempts'][0]==old and len(record['attempts'])==2


def test_path_diagnostics_share_targets_and_group_collisions_once():
    target=answer()['pages'][0];pages=[]
    for n in range(12):
        page=copy.deepcopy(target);page['identity']['identity_key']='distinct-'+str(n);pages.append(page)
    draft={'pages':pages,'detail_unit_refs':[],'reason':'Isolated many-way collision'}
    d=output.diagnosis('global-plan',{'proposal_refs':['p1'],'units':[], 'view':{'coordination':{'planned_pages':[target]}}},draft,ContractError('IDENTITY_CONFLICT','$','collision'))
    assert len(d['offered_targets'])==1 and d['failed_response']==draft
    groups=[f for f in d['page_findings'] if f['rule']=='distinct_identities_share_path']
    assert len(groups)==1 and groups[0]['output_indices']==list(range(12))


@pytest.mark.parametrize('phase',['global-plan','global-detail'])
@pytest.mark.parametrize('collision',['identity','path'])
def test_diagnosis_checks_accumulated_reservations_without_offering_them(phase,collision):
    draft=answer(True);page=draft['pages'][0]
    target=copy.deepcopy(page);target['identity']['canonical_name']='Previously planned name'
    if collision=='identity':target['path']='30_Cards/previously-planned.md'
    else:target['identity']['identity_key']='different-reserved-key'
    target['candidate_refs']=['never-disclose-membership'];target['content']='never-disclose-body'
    state={'pages':[target,*[{'identity':{'kind':'fact','identity_key':'unrelated-'+str(i)},'path':'unrelated/'+str(i)+'.md'} for i in range(100)]]}
    packet={'proposal_refs':['p1','p2'],'candidate_refs':['c1','c2'],'units':[], 'view':{'coordination':{'planned_pages':[]}}}
    before=copy.deepcopy((draft,packet,state))
    d=output.diagnosis(phase,packet,draft,ContractError('DUPLICATE','$','earlier coverage error'),state=state)
    f=next(f for f in d['page_findings'] if f['rule']=='occupied_planned_target_outside_offered_directory')
    assert f['colliding_fields']==[collision] and f['output_index']==0 and f['different_fields']
    assert len(d['reserved_targets'])==1 and not d['offered_targets']
    reserved=d['reserved_targets'][f['reserved_target_ref']]
    assert reserved['identity']==target['identity'] and reserved['append_allowed'] is False
    assert 'never-disclose' not in json.dumps(d) and 'unrelated-' not in json.dumps(d)
    assert (draft,packet,state)==before


def test_diagnosis_does_not_reclassify_offered_target_as_reserved():
    target=answer()['pages'][0];draft=answer();draft['pages'][0]['identity']['aliases']=['not the offered aliases']
    packet={'proposal_refs':['p1','p2'],'units':[], 'view':{'coordination':{'planned_pages':[target]}}}
    d=output.diagnosis('global-plan',packet,draft,ContractError('IDENTITY_CONFLICT','$','append mismatch'),state={'pages':[target]})
    assert d['reserved_targets']=={} and d['offered_targets']
    assert {f['rule'] for f in d['page_findings']}=={'append_requires_exact_offered_identity_and_path'}


def test_reserved_diagnostic_metadata_never_grants_append_authority():
    value=fixture_value();target=answer()['pages'][0]
    state=g.initial();state['pages']=[{'identity':target['identity'],'path':target['path'],'candidate_refs':['already-routed'] }]
    packet=g.next_packet(value,g.units(value),state,fixed.prepare_request_projection)
    assert not packet['view']['coordination']['planned_pages']
    draft=answer();diagnostic=output.diagnosis('global-plan',packet,draft,ContractError('DUPLICATE','$','another finding'),state=state)
    assert diagnostic['reserved_targets']
    with pytest.raises(ContractError,match='exact offered'):g.accept_proposals(value,state,packet,draft)
