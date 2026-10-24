"""Finite explicit recovery without hiding old unknown calls or coverage gaps."""
import copy
import hashlib
import json
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
import semantic_output as output
import reduce_disposition as disposition
from test_semantic_output import fixture, answer


def grant(c,action='revise-output'):
    path=output.path_for(c.adapter,c.binding,'global-plan',c.packet['view'])
    record=json.loads(path.read_text());attempts=record['attempts']
    row={'journal_name':path.name,'phase':'global-plan','action':action,
        'input_sha256':record['input_sha256'],'schema_sha256':record['schema_sha256'],
        'preserved_attempt_count':len(attempts),'prior_attempts_sha256':fingerprint(attempts),
        'attempt_ordinal':len(attempts)+1,'additional_calls':1,
        'correction':{'contract':disposition.CONTRACT,'instruction':'Use the supplied exact target references.'}}
    report=path.parent.parent/'explicit-recovery.json'
    report.write_text(json.dumps({'request':{'recovery_scope':disposition.SCOPE},
        'entries':[{'node':'global-reduce','output_dispositions':[row]}]}))
    pointer={'report_ref':report.relative_to(c.adapter.workflow.vault).as_posix(),
        'report_sha256':hashlib.sha256(report.read_bytes()).hexdigest()}
    c.adapter.workflow.status=lambda _:{'execution_recovery_history':[pointer]}
    return path,copy.deepcopy(attempts),pointer


def test_explicit_revision_appends_once_and_preserves_exhausted_failures(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True),answer()])
    with pytest.raises(ContractError):c.run()
    path,old,_=grant(c)
    assert c.run()[0]==answer() and len(c.calls)==3
    after=json.loads(path.read_text())['attempts']
    assert after[:2]==old and after[2]['attempt']==3
    assert after[2]['correctable'] is False


def test_explicit_regeneration_preserves_unknown_and_never_retries_it(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[ContractError('NATIVE_COMPLETION_UNKNOWN','$','unknown'),answer()])
    with pytest.raises(ContractError):c.run()
    path,old,_=grant(c,'regenerate-output')
    assert c.run()[0]==answer() and len(c.calls)==2
    after=json.loads(path.read_text())['attempts']
    assert after[:1]==old and old[0]['diagnostic']['code']=='NATIVE_COMPLETION_UNKNOWN'
    assert old[0]['session_id']=='uncertain-native' and not old[0].get('audit_ref')


def test_failed_explicit_revision_has_no_automatic_correction(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True),answer(True)])
    with pytest.raises(ContractError):c.run()
    path,old,_=grant(c)
    with pytest.raises(ContractError):c.run()
    with pytest.raises(ContractError,match='explicit recovery exhausted'):c.run()
    assert len(c.calls)==3 and json.loads(path.read_text())['attempts'][:2]==old


@pytest.mark.parametrize('action',['defer','exclude-covered'])
def test_nonexecution_dispositions_do_not_consume_a_call(tmp_path,monkeypatch,action):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True)])
    with pytest.raises(ContractError):c.run()
    path,old,_=grant(c,action)
    with pytest.raises(ContractError,match='disposition holds'):c.run()
    assert len(c.calls)==2 and json.loads(path.read_text())['attempts']==old


def test_prior_attempt_tamper_and_duplicate_grants_are_rejected(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True)])
    with pytest.raises(ContractError):c.run()
    path,old,pointer=grant(c)
    c.adapter.workflow.status=lambda _:{'execution_recovery_history':[pointer,pointer]}
    with pytest.raises(ContractError,match='one explicit disposition'):c.run()
    c.adapter.workflow.status=lambda _:{'execution_recovery_history':[pointer]}
    record=json.loads(path.read_text());record['attempts'][0]['session_id']='tampered'
    path.write_text(json.dumps(record))
    with pytest.raises(ContractError,match='sealed original'):c.run()
    assert len(c.calls)==2


def test_cancel_after_grant_before_reservation_does_not_use_budget(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True),answer()])
    with pytest.raises(ContractError):c.run()
    path,old,_=grant(c)
    c.adapter.worker_check=lambda _:(_ for _ in ()).throw(ContractError('WORKFLOW_STOPPED','$','stop'))
    with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):c.run()
    assert len(c.calls)==2 and json.loads(path.read_text())['attempts']==old


def transition(c,path,pointer,action='revise-output'):
    prior=disposition.output_permit(c.adapter,c.binding,path,json.loads(path.read_text()))
    evidence=c.adapter.workflow.vault/'review.json';evidence.write_text('{"scope":"isolated corrected cause"}')
    basis=[{'ref':evidence.name,'sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}]
    record=json.loads(path.read_text())
    row={k:v for k,v in prior.items() if k!='authorization_ref'}
    row.update(supersedes=disposition.predecessor(prior),basis=basis,action=action,
        preserved_attempt_count=len(record['attempts']),prior_attempts_sha256=fingerprint(record['attempts']),
        attempt_ordinal=len(record['attempts'])+1,additional_calls=int(action=='revise-output'))
    row['correction']={**row['correction'],'targeted_clarification':'Check assigned claims only.'}
    report=path.parent.parent/'next-recovery.json'
    report.write_text(json.dumps({'request':{'recovery_scope':disposition.SCOPE},
        'entries':[{'node':'global-reduce','output_dispositions':[row]}]}))
    current={'report_ref':report.relative_to(c.adapter.workflow.vault).as_posix(),
        'report_sha256':hashlib.sha256(report.read_bytes()).hexdigest()}
    c.adapter.workflow.status=lambda _:{'execution_recovery_history':[pointer,current]}
    return row,prior,evidence


def test_deferred_output_can_resume_with_sealed_new_basis_without_reset(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True),answer()])
    with pytest.raises(ContractError):c.run()
    path,old,pointer=grant(c,'defer')
    # Historical nonexecution grants have zero additional calls.
    report=tmp_path/pointer['report_ref'];payload=json.loads(report.read_text())
    payload['entries'][0]['output_dispositions'][0]['additional_calls']=0
    report.write_text(json.dumps(payload));pointer['report_sha256']=hashlib.sha256(report.read_bytes()).hexdigest()
    row,prior,evidence=transition(c,path,pointer)
    disposition.transition_basis(c.adapter,{**row,'clarification':'Check assigned claims only.'},prior,json.loads(path.read_text()))
    assert c.run()[0]==answer() and len(c.calls)==3
    assert json.loads(path.read_text())['attempts'][:2]==old
    evidence.write_text('changed')
    with pytest.raises(ContractError,match='transition evidence changed'):c.run()


def test_transition_rejects_fork_unused_authority_and_missing_new_basis(tmp_path,monkeypatch):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True)])
    with pytest.raises(ContractError):c.run()
    path,_,pointer=grant(c)
    record=json.loads(path.read_text());prior=disposition.output_permit(c.adapter,c.binding,path,record)
    row={'supersedes':disposition.predecessor(prior),'clarification':'A different ID is insufficient.'}
    with pytest.raises(ContractError,match='previous authority'):disposition.transition_basis(c.adapter,row,prior,record)
    prior={**prior,'additional_calls':0};row['supersedes']=disposition.predecessor(prior)
    with pytest.raises(ContractError,match='persisted new evidence'):disposition.transition_basis(c.adapter,row,prior,record)


@pytest.mark.parametrize('used',[False,True])
def test_defer_records_failed_or_surrenders_unused_recovery_without_new_cause(tmp_path,monkeypatch,used):
    c=fixture(tmp_path,monkeypatch,[answer(True),answer(True),answer(True),answer()])
    with pytest.raises(ContractError):c.run()
    path,old,pointer=grant(c)
    if used:
        with pytest.raises(ContractError):c.run()
    record=json.loads(path.read_text())
    prior=disposition.output_permit(c.adapter,c.binding,path,record)
    row={k:v for k,v in prior.items() if k!='authorization_ref'}
    row.update(action='defer',additional_calls=0,supersedes=disposition.predecessor(prior),
        preserved_attempt_count=len(record['attempts']),prior_attempts_sha256=fingerprint(record['attempts']),
        forfeited_previous_calls=int(not used),basis=None)
    assert disposition.transition_basis(c.adapter,row,prior,record)==(None,None)
    report=path.parent.parent/'deferred-recovery.json'
    report.write_text(json.dumps({'request':{'recovery_scope':disposition.SCOPE},
        'entries':[{'node':'global-reduce','output_dispositions':[row]}]}))
    later={'report_ref':report.relative_to(c.adapter.workflow.vault).as_posix(),
        'report_sha256':hashlib.sha256(report.read_bytes()).hexdigest()}
    c.adapter.workflow.status=lambda _:{'execution_recovery_history':[pointer,later]}
    before=path.read_bytes()
    with pytest.raises(ContractError,match='disposition holds'):c.run()
    assert path.read_bytes()==before and len(c.calls)==2+int(used)
    # Surrender does not refund the original grant. The complete history still
    # includes it and any later recovery needs a new pinned cause.
    permit=disposition.output_permit(c.adapter,c.binding,path,record)
    again={'supersedes':disposition.predecessor(permit),'action':'revise-output','clarification':'Retry'}
    with pytest.raises(ContractError,match='persisted new evidence'):
        disposition.transition_basis(c.adapter,again,permit,record)
    row['forfeited_previous_calls']=1 if used else 0
    report.write_text(json.dumps({'request':{'recovery_scope':disposition.SCOPE},
        'entries':[{'node':'global-reduce','output_dispositions':[row]}]}))
    later['report_sha256']=hashlib.sha256(report.read_bytes()).hexdigest()
    with pytest.raises(ContractError,match='erase consumed attempts'):
        disposition.output_permit(c.adapter,c.binding,path,record)
