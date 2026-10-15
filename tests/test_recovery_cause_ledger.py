"""Admission and replay validate the same finite recovery causes."""
import hashlib,json
from types import SimpleNamespace
import pytest
from hermes_source_units.execution_plan import ExecutionPlanMixin,PACKET_CAUSES
from hermes_source_units import ContractError


@pytest.mark.parametrize('change',['distinct','repeated','unknown','tampered','mechanical_repeat'])
def test_recovery_ledger_rejects_repeated_causes_without_rejecting_distinct_repairs(tmp_path,change):
    workflow={'workflow_id':'isolated','actor':'operator','batch_id':None,'execution_recovery_history':[]}
    first={'error_code':'ACCESS_DENIED','reason':PACKET_CAUSES['ACCESS_DENIED']}
    second={'error_code':'PROGRAM_EXECUTION_FAILED','reason':"'expected_revision'"}
    if change=='repeated':second=first
    if change=='unknown':second={'error_code':'PROGRAM_EXECUTION_FAILED','reason':'unknown failure'}
    scope='mechanical/v1' if change=='mechanical_repeat' else 'revision-citation-packet/v1'
    for index,failure in enumerate([first,second]):
        request={'workflow_id':'isolated','actor':'operator','recovery_id':str(index),'input_digest':str(index),'recovery_scope':scope}
        report={'request':request,'entries':[{'node':'pass-slice:same','failure':failure,'revision_packet_identity':{'packet_ref':'native-packet'}}]}
        data=json.dumps(report).encode();ref=f'{index}.json';(tmp_path/ref).write_bytes(data)
        workflow['execution_recovery_history'].append({'recovery_id':str(index),'input_digest':str(index),'nodes':['pass-slice:same'],
            'report_ref':ref,'report_sha256':hashlib.sha256(data).hexdigest()})
    if change=='tampered':(tmp_path/'1.json').write_text('{}')
    service=SimpleNamespace(vault=tmp_path,verify_pass_skips=lambda _:None,
        _execution_journal=lambda _:tmp_path/'absent',execution_plan=lambda _:None)
    if change=='distinct':ExecutionPlanMixin.assert_execution_ready(service,workflow)
    else:
        with pytest.raises(ContractError,match='STALE_INPUT'):ExecutionPlanMixin.assert_execution_ready(service,workflow)
