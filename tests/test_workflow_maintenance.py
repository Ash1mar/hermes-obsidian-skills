"""Isolated maintenance contracts: no production workflows or model calls."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/lib'))
from hermes_source_units import ContractError
from hermes_source_units.recovery_policy import DEFAULT,effective,validate
from hermes_source_units.recovery_control import automatic,check,epoch,record_cancel
from hermes_source_units.validation import validate_record
import recovery_controller as controller
import source_resource_recovery as resource
import workflow_metrics as metrics


def test_new_policy_is_pinned_legacy_fallback_is_explicit():
    w={'scope':{'recovery_policy':copy.deepcopy(DEFAULT)}}
    assert effective(w,{'pass_failure_policy':'hold','qa_policy':{}})==DEFAULT
    assert effective({'scope':{}},{'pass_failure_policy':'hold'})['semantic_failure']=='hold'
    assert effective({'scope':{}},{})['source_resource_retries']==0
    for field,value in [('source_resource_retries',2),('controller_attempts',99),('unknown_completion','retry'),('qa',{})]:
        with pytest.raises(ContractError):validate({**DEFAULT,field:value})
    validate_record('ingest_workflow_scope',{'source_paths':[],'knowledge_selector':'test','recovery_policy':DEFAULT})


def test_manual_cancel_invalidates_automatic_continuation_after_restart(tmp_path):
    before=epoch(tmp_path,'w')
    # Program-owned cancellation preserves the operator epoch.
    q={'workflow_id':'w','actor':'a','expected_revision':4,'input_digest':'first'}
    with automatic(tmp_path,'w',before):record_cancel(tmp_path,q)
    assert epoch(tmp_path,'w') is None
    record_cancel(tmp_path,{**q,'input_digest':'user-stop'})
    with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):
        with automatic(tmp_path,'w',before):check(tmp_path,'w')


def test_operator_stop_blocks_supported_mutation_before_reading_domain(tmp_path):
    from hermes_source_units.ingest_workflow import FileIngestWorkflowService
    service=object.__new__(FileIngestWorkflowService);service.vault=tmp_path
    service._check_request=lambda _:None
    service._load=lambda _:pytest.fail('cancelled controller must not enter domain mutation')
    with automatic(tmp_path,'w',None):
        # Simulate an independently authenticated operator cancellation.
        from hermes_source_units.recovery_control import stop_path
        path=stop_path(tmp_path,'w');path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'input_digest':'stop'}))
        with pytest.raises(ContractError,match='WORKFLOW_STOPPED'):
            service._mutation({'workflow_id':'w'})


def adapter(tmp_path):
    w={'workflow_id':'w','current_stage':'analyzing','state':'analyzing','revision':7,
       'scope':{'recovery_policy':copy.deepcopy(DEFAULT)},'cancel_requested':False,
       'kanban':{'task_map':[{'node':'pass-slice:s','task_id':'t'}]}}
    return NS(workflow=NS(vault=tmp_path,status=lambda _:copy.deepcopy(w),pending_pause=lambda _:False)),w


def test_supervisor_consumes_finite_attempts_and_reuses_episode(tmp_path,monkeypatch):
    a,w=adapter(tmp_path);key=controller.prepare(a,'w');assert controller.prepare(a,'w')==key
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        Path(command[-1]).write_text(json.dumps({'ok':False,'code':'LOCK_BUSY'}))
        return NS(returncode=0)
    monkeypatch.setattr(controller.subprocess,'run',run)
    monkeypatch.setattr(controller.time,'sleep',lambda _:None)
    result=controller.supervise(a,'w',key,'unused.py')
    assert result['state']=='held' and result['attempts']==3
    assert controller.supervise(a,'w',key,'unused.py')==result and len(calls)==3


def test_supervisor_preserves_reserved_budget_after_interruption(tmp_path,monkeypatch):
    a,w=adapter(tmp_path);key=controller.prepare(a,'w')
    root=tmp_path/f'_system/ledgers/ingest-workflows/w/automatic-control'
    path=root/(key+'.json');s=json.loads(path.read_text());s.update(attempts=2,state='running');path.write_text(json.dumps(s))
    calls=[]
    def run(command,**kwargs):
        calls.append(command);Path(command[-1]).write_text(json.dumps({'ok':True,'applied':True}));return NS(returncode=0)
    monkeypatch.setattr(controller.subprocess,'run',run)
    assert controller.supervise(a,'w',key,'unused.py')['attempts']==3 and len(calls)==1
    w['cancel_requested']=True
    # This finished episode cannot provide automatic authority for a new stop.
    with pytest.raises(ContractError):controller.prepare(a,'w')


def test_supervisor_holds_unknown_and_operator_stop_without_replay(tmp_path,monkeypatch):
    a,w=adapter(tmp_path);key=controller.prepare(a,'w')
    def run(command,**kwargs):
        Path(command[-1]).write_text(json.dumps({'ok':False,'code':'EXECUTION_UNCERTAIN'}));return NS(returncode=2)
    monkeypatch.setattr(controller.subprocess,'run',run)
    assert controller.supervise(a,'w',key,'unused.py')['attempts']==1
    w['kanban']['task_map'][0]['task_id']='new';key=controller.prepare(a,'w')
    record_cancel(tmp_path,{'workflow_id':'w','actor':'a','expected_revision':7,'input_digest':'manual'})
    monkeypatch.setattr(controller.subprocess,'run',lambda *a,**k:pytest.fail('cancelled owner must not launch'))
    assert controller.supervise(a,'w',key,'unused.py')['reason']=='operator_cancelled'


def test_supervisor_consumes_completed_child_receipt_after_parent_crash(tmp_path,monkeypatch):
    a,w=adapter(tmp_path);key=controller.prepare(a,'w')
    root=tmp_path/'_system/ledgers/ingest-workflows/w/automatic-control'
    path=root/(key+'.json');s=json.loads(path.read_text());s.update(attempts=1,state='running');path.write_text(json.dumps(s))
    (root/(key+'-attempt-1.json')).write_text(json.dumps({'ok':True,'applied':True}))
    monkeypatch.setattr(controller.subprocess,'run',lambda *a,**k:pytest.fail('completed child must not replay'))
    result=controller.supervise(a,'w',key,'unused.py')
    assert result['state']=='applied' and result['attempts']==1


@pytest.mark.parametrize('attempts',[1,3])
def test_corrected_unknown_contract_reuses_no_action_episode_without_budget_reset(tmp_path,monkeypatch,attempts):
    a,w=adapter(tmp_path);w['current_stage']='reducing';w['scope']['execution_mode']='auto_full'
    key=controller.prepare(a,'w');path=tmp_path/f'_system/ledgers/ingest-workflows/w/automatic-control/{key}.json'
    row=json.loads(path.read_text());row.update(state='no_action',attempts=attempts);path.write_text(json.dumps(row))
    monkeypatch.setattr('source_unknown_recovery.select',lambda *_:[{'node':'resource-reduce:r'}])
    monkeypatch.setattr('source_unknown_recovery.contract_hash',lambda:'corrected-contract')
    assert controller.prepare(a,'w')==key
    result=json.loads(path.read_text());assert result['attempts']==attempts
    assert result['state']==('pending' if attempts==1 else 'no_action')
    if attempts==1:
        result['state']='no_action';path.write_text(json.dumps(result))
        controller.prepare(a,'w');assert json.loads(path.read_text())['state']=='no_action'


def diagnostic(path,**changes):
    path.mkdir(parents=True,exist_ok=True)
    row={'schema_version':'hermes-conversion-result/v1','source_sha256':'a'*64,'backend':'hybrid-engine',
         'status':'runtime_failed','resource_failure':'cuda_out_of_memory','engine_exit':1}
    (path/'conversion-result.json').write_text(json.dumps({**row,**changes}))


@pytest.mark.parametrize('change',[{'status':'document_failed'},{'resource_failure':None},{'engine_exit':-9},{'engine_exit':None}])
def test_resource_retry_does_not_treat_quality_or_unknown_as_resource(tmp_path,change):
    c=tmp_path/'bundle';diagnostic(c,**change)
    out=resource.recover(c,tmp_path,{'content_sha256':'a'*64},'hybrid-engine',
        lambda *a:pytest.fail('must not retry'),lambda:None,allowed=True)
    assert out==c


def test_resource_retry_once_across_restart_and_backends(tmp_path):
    c=tmp_path/'attempt-1';diagnostic(c);calls=[]
    def invoke(path,log):calls.append(path);diagnostic(path,status='converted',resource_failure=None);return NS(returncode=0)
    args=(c,tmp_path,{'content_sha256':'a'*64},'hybrid-engine',invoke,lambda:None)
    retry=resource.recover(*args,allowed=True)
    assert resource.recover(*args,allowed=True)==retry and len(calls)==1
    other=tmp_path/'attempt-2';diagnostic(other,backend='pipeline')
    assert resource.recover(other,tmp_path,{'content_sha256':'a'*64},'pipeline',invoke,lambda:None,allowed=True)==other
    assert len(calls)==1


def test_resource_reserved_unknown_does_not_repeat(tmp_path):
    c=tmp_path/'attempt-1';diagnostic(c)
    def interrupted(*a):raise RuntimeError('crash')
    args=(c,tmp_path,{'content_sha256':'a'*64},'hybrid-engine',interrupted,lambda:None)
    with pytest.raises(RuntimeError):resource.recover(*args,allowed=True)
    with pytest.raises(ContractError,match='unknown completion'):resource.recover(*args,allowed=True)


def test_resource_failure_must_be_typed_terminal_cuda_traceback():
    import importlib.util
    p=ROOT/'hermes-obsidian-controlled-ingest/scripts/convert_pdf_with_mineru_bundle.py'
    spec=importlib.util.spec_from_file_location('resource_converter',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    text='Traceback (most recent call last):\n  call()\ntorch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB.'
    assert m.gpu_resource_failure(text)=='cuda_out_of_memory'
    assert m.gpu_resource_failure('note CUDA out of memory') is None
    assert m.gpu_resource_failure(text+'\nRuntimeError: other failure') is None


def event(name,start,end,**fields):
    return {'name':name,'span_id':name+str(start),'started_at':start,'finished_at':end,
        'elapsed_ms':(end-start)*1000,'error':None,'pid':1,'clock_domain':'boot','session_id':'s','phase':'citation',**fields}


def test_metrics_half_open_windows_dedup_and_unknown_samples(tmp_path):
    rows=[event('sdk_request_start',0,0),event('sdk_model_request',0,10),
          event('full_reconciliation',1,2),event('full_reconciliation',2,4,error='LOCK_BUSY')]
    report=metrics.window(rows,2,8,'active')
    assert report['sdk']['average']==1 and report['sdk']['zero_inflight_fraction']==0
    assert report['cross_cutoff_spans']==1
    assert report['program_span_ms']['full_reconciliation']['failed_ms']['n']==1
    assert metrics.distribution([])['status']=='no_sample'
    rows=[{**r,'vault':str(tmp_path),'workflow_id':'w'} for r in rows]
    (tmp_path/'1.jsonl').write_text('\n'.join(json.dumps(r) for r in rows+rows)+ '\ninvalid')
    found,diagnostics=metrics.events_from(tmp_path,tmp_path,'w')
    assert len(found)==4 and diagnostics=={'invalid_rows':1,'duplicate_rows_ignored':4}


def test_metrics_terminal_snapshot_does_not_inherit_historical_cancelled_state(tmp_path):
    timeline=tmp_path/'timeline';timeline.mkdir()
    w={'workflow_id':'w','batch_id':None,'revision':69,'state':'partial','current_stage':'validating','scope':{},'source_outcomes':[]}
    s=NS(vault=tmp_path,status=lambda _:w)
    report=metrics.collect(s,'w',timeline=timeline)
    assert report['registry']['S04']['value']['revision']==69
    assert report['registry']['Z01']['value']['state']=='partial'
    assert report['registry']['M07']['status']=='unknown' and report['usage']['unique_sessions']==0
    assert report['current_snapshot']['native_settlement'].startswith('unknown')
    assert len(report['registry'])==100


def test_metrics_report_detects_snapshot_change(tmp_path):
    w={'workflow_id':'w','batch_id':None,'revision':1,'state':'planning','current_stage':'planning','scope':{},'source_outcomes':[]}
    states=iter([w,{**w,'revision':2}]);s=NS(vault=tmp_path,status=lambda _:next(states))
    assert metrics.collect(s,'w',timeline=tmp_path)['snapshot_consistent'] is False


def test_metrics_task_status_and_pass_records_are_scoped_to_current_batch(tmp_path):
    for tid in ('one','other-workflow'):
        p=tmp_path/f'_system/knowledge-builds/task-{tid}/passes/p.json';p.parent.mkdir(parents=True);p.write_text('{}')
    batch={'task_ids':['one','two'],'slice_config':{'pass_worker_concurrency':5}}
    w={'workflow_id':'w','batch_id':'batch','revision':1,'state':'analyzing','scope':{},'source_outcomes':[]}
    knowledge=NS(_batch=lambda _:batch,_slices=lambda _:[],
        _task=lambda tid:{'status':'completed' if tid=='one' else 'failed'})
    report=metrics.collect(NS(vault=tmp_path,status=lambda _:w,knowledge=knowledge),'w',timeline=tmp_path)
    value=report['registry']['W04']['value']
    assert value['tasks']=={'completed':1,'failed':1} and value['pass_records']==1
    assert report['current_snapshot']['current_pass_capacity']['limit']==5
