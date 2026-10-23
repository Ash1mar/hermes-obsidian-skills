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
    assert after[:1]==old and old[0]['state']=='reserved' and old[0]['session_id']=='uncertain-native'


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
