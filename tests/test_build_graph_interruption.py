"""Strict native pre-execution interruption admission, isolated from the Vault."""
import copy,json
from types import SimpleNamespace as NS
import pytest
import build_execution_recovery as recovery
from hermes_source_units import ContractError

def fixture(tmp_path):
    binding={'workflow_id':'fixture','task_id':'uncalled'}
    failure={'error_code':'SOURCE_CHANGED','reason':recovery.GRAPH_INTERRUPTION}
    run={'id':1,'profile':'ingest-program','status':'scheduled','outcome':'scheduled',
         'summary':'Vault workflow cancelled','ended_at':2,'worker_pid':123}
    task={'id':'uncalled','status':'archived','created_by':'ingest-workflow','session_id':None}
    native={'task':task,'runs':[run]}
    adapter=NS(workflow=NS(vault=tmp_path,status=lambda _: {'kanban':{'board_id':'board'}}),
               kanban=NS(task_snapshot=lambda *_:native))
    return adapter,binding,failure,native

def test_positive_scheduled_cancellation_grants_no_domain_calls(tmp_path):
    a,b,f,n=fixture(tmp_path)
    proof=recovery.graph_interruption(a,b,f,n['runs'])
    assert proof['new_domain_calls_granted']==0 and proof['original_review_history_preserved']

@pytest.mark.parametrize('change',['reason','code','session','live','run-outcome','unended','missing-run','audit','unknown','ordinal'])
def test_uncertainty_or_domain_execution_is_not_a_graph_retry(tmp_path,change):
    a,b,f,n=fixture(tmp_path)
    if change=='reason':f['reason']='Source content changed'
    elif change=='code':f['error_code']='NATIVE_COMPLETION_UNKNOWN'
    elif change=='session':n['task']['session_id']='uncertain'
    elif change=='live':n['task']['worker_pid']=456
    elif change=='run-outcome':n['runs'][0]['outcome']='failed'
    elif change=='unended':n['runs'][0]['ended_at']=None
    elif change=='missing-run':n['runs']=[]
    else:
        folder,record={'audit':('semantic-calls',{'native_task_id':b['task_id']}),
            'unknown':('call-failures',{'task_id':b['task_id'],'state':'completion_unknown'}),
            'ordinal':('output-attempts',{'attempts':[{'task_id':b['task_id'],'state':'reserved'}]})}[change]
        path=tmp_path/f'_system/ledgers/ingest-workflows/fixture/{folder}/evidence.json'
        path.parent.mkdir(parents=True);path.write_text(json.dumps(record))
    with pytest.raises(ContractError):recovery.graph_interruption(a,b,f,n['runs'])
