"""Source retry reuses only exact native zero-consumption jobs."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/lib'),str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/scripts'),str(ROOT/'hermes-source-units/src')]
import incremental_source_retry as module
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint


@pytest.mark.parametrize('outcome,started,allowed', [('scheduled', False, False), ('crashed', False, False),
                                                     ('scheduled', True, False), ('crashed', True, True)])
def test_preparation_operator_retry_requires_matching_abnormal_worker_run(tmp_path, monkeypatch, outcome, started, allowed):
    import incremental_ingest_controller as controller
    import incremental_preparation_retry as preparation
    import ingest_kanban
    import program_worker
    from hermes_source_units.source_units import _sha

    relative = '10_Raw/sample.pdf'
    raw = tmp_path / relative
    raw.parent.mkdir()
    raw.write_bytes(b'fixture')
    digest = _sha(raw.read_bytes())
    node = 'source-prepare:' + fingerprint({'path': relative, 'content_sha256': digest})[:16]
    claim = {'workflow_id': 'origin', 'node': node, 'input_fingerprint': 'inputs', 'task_id': 'card'}
    card = {'node': node, 'task_id': 'card', 'idempotency_key': 'slot'}
    folder = tmp_path / '_system/ledgers/ingest-workflows/origin'
    binding = folder / 'bindings' / (fingerprint('slot') + '.json')
    if started:
        _write_atomic(binding.with_suffix('.execution.json'), _json_bytes({'run_id': 7, 'state': 'running',
            'executor': 'program-v1', 'grant': {'worker_pid': 51, 'claim_lock': 'past-claim'}}))
    task = SimpleNamespace(id='card', status='scheduled', claim_lock=None, worker_pid=None, created_by='ingest-workflow',
                           body=json.dumps({**claim, 'worker_binding': str(binding)}))
    ended = SimpleNamespace(id=7, outcome=outcome, started_at=1, ended_at=2 if started else 1)
    dispatched = []

    def unblock(_db, identity):
        dispatched.append(identity)
        return True

    native = SimpleNamespace(get_task=lambda *_: task, list_runs=lambda *_a, **_kw: [ended], unblock_task=unblock)
    monkeypatch.setitem(sys.modules, 'hermes_cli', SimpleNamespace(kanban_db=native))
    monkeypatch.setattr(preparation, 'completion_connection', lambda _: SimpleNamespace(close=lambda: None))
    workflow = {'cancel_requested': False, 'actor': 'operator', 'batch_id': None, 'workflow_id': 'origin',
                'scope': {'incremental_run_id': 'run'}, 'source_outcomes': [],
                'kanban': {'board_id': 'origin', 'task_map': [card]}}
    run = {'actor': 'operator', 'state': 'active', 'revision': 1, 'source_workflow_id': 'origin',
           'source_manifest': [{'path': relative, 'sha256': digest}], 'sources': {relative: {'state': 'pending'}}}
    head = {'actor': 'operator', 'state': 'active', 'revision': 1}
    adapter = SimpleNamespace(workflow=None, _source_window=lambda *_: {node}, worker_check=lambda *_: None)
    monkeypatch.setattr(ingest_kanban, 'IngestKanbanAdapter', lambda *_a, **_k: adapter)
    monkeypatch.setattr(ingest_kanban, 'desired_graph', lambda *_: [])
    monkeypatch.setattr(program_worker, 'canonical_binding', lambda *_: (claim, None))
    monkeypatch.setattr(preparation, 'FileIngestWorkflowService', lambda *_: SimpleNamespace(status=lambda _: workflow))
    monkeypatch.setattr(preparation, 'IncrementalControl', lambda *_: SimpleNamespace(status=lambda: head))
    monkeypatch.setattr(controller, 'IncrementalControl', preparation.IncrementalControl)
    monkeypatch.setattr(controller, 'attach_sources', lambda *_: None)
    monkeypatch.setattr(controller, 'spawn', lambda *_: None)
    domain = SimpleNamespace(vault=tmp_path, root=lambda _: tmp_path / 'run', status=lambda _: run)
    request = {'run_id': 'run', 'actor': 'operator', 'operation_id': 'retry', 'control_revision': 1,
               'run_revision': 1, 'source_path': relative, 'source_sha256': digest, 'retry_kind': preparation.KIND}
    if allowed:
        assert preparation.retry(domain, request)['prior_run_id'] == 7
        assert dispatched == ['card']
    else:
        with pytest.raises(ContractError, match='no evidenced interrupted worker'):
            preparation.retry(domain, request)
        assert not dispatched
        assert not list((tmp_path / 'run/preparation-retries').glob('*.json'))


def test_preparation_retry_claim_preserves_original_and_is_bound_to_one_native_run(tmp_path, monkeypatch):
    from incremental_preparation_retry import CONTRACT, conversion_generation

    source = {'path': '10_Raw/first.pdf', 'content_sha256': 'a' * 64}
    binding = {'workflow_id': 'workflow', 'node': 'source-prepare:a', 'task_id': 'task-a', 'template_hash': 'template'}
    workflow = SimpleNamespace(vault=tmp_path, status=lambda _: {'scope': {'incremental_run_id': 'incremental'}})
    adapter = SimpleNamespace(workflow=workflow)
    root = tmp_path / '_system/ledgers/incremental-ingest/incremental/preparation-retries'
    path = root / (fingerprint(source['path']) + '.json')
    request = {'operation_id': 'recovery-a', 'run_id': 'incremental', 'source_path': source['path'],
               'source_sha256': source['content_sha256']}
    assert conversion_generation(adapter, binding, source) == ''
    grant = {'contract': CONTRACT, 'request': request, 'binding': binding, 'board': 'board-a', 'prior_run_id': 9}
    _write_atomic(path, _json_bytes(grant))
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'board-a')
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'task-a')
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '9')
    with pytest.raises(ContractError, match='escaped its native task'):
        conversion_generation(adapter, binding, source)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '10')
    assert conversion_generation(adapter, binding, source) == 'recovery-a'
    assert conversion_generation(adapter, binding, source) == 'recovery-a'
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '11')
    with pytest.raises(ValueError, match='different identity'):
        conversion_generation(adapter, binding, source)
    assert json.loads(path.read_bytes()) == grant
    assert json.loads(path.with_suffix('.started.json').read_bytes())['run_id'] == 10


def fixture(tmp_path,monkeypatch):
    import run_short_evidence_test as native
    import native_stop_inventory
    import ingest_kanban
    import incremental_ingest_controller as controller
    run_id='retry-source-fixture'
    path='10_Raw/a.pdf'
    raw_hash='a'*64
    snapshot_id='snapshot-a'
    root=tmp_path/'_system/ledgers/incremental-ingest'/run_id
    workspace=root/'native/source'/snapshot_id/'mapping'
    operation={'contract':native.CONTRACT,'workflow_mode':'incremental_v1','vault':str(tmp_path),'actor':'operator','board':'retry-board','incremental':{'run_id':run_id,'scope_kind':'source','scope_id':snapshot_id,'stage':'mapping','snapshots':[snapshot_id]}}
    _write_atomic(workspace/'operation.json',_json_bytes(operation))
    row={'id':'request-a','phase':'short-topics'}
    job=workspace/'jobs/request-a.json'
    _write_atomic(job,_json_bytes({'request':row}))
    task=SimpleNamespace(id='native-a',body=native.task_body(workspace,job),status='blocked',claim_lock=None,assignee=native.ASSIGNEE,created_by=native.CONTRACT,workspace_kind='dir',workspace_path=str(workspace))
    ended=SimpleNamespace(id=3,status='gave_up',ended_at=1,outcome='gave_up')
    run={'actor':'operator','revision':7,'state':'active','sources':{path:{'state':'prepared','snapshot_id':snapshot_id}},'source_manifest':[{'path':path,'sha256':raw_hash}],'reservations':{}}
    control={'actor':'operator','revision':5,'state':'paused','active_scopes':{},'boundary':{'stage':'mapping'}}
    domain=SimpleNamespace(vault=tmp_path,root=lambda _:root,status=lambda _:run,validate_source_snapshot=lambda *_:{'original':{'ref':path,'sha256':raw_hash}})
    monkeypatch.setattr(module,'service',lambda *_:domain)
    monkeypatch.setattr(module,'IncrementalControl',lambda *_:SimpleNamespace(status=lambda:control))
    monkeypatch.setattr(controller,'IncrementalControl',module.IncrementalControl)
    monkeypatch.setattr(module,'profile_ready',lambda:True)
    monkeypatch.setattr(module,'controller_alive',lambda _:False)
    monkeypatch.setattr(module,'completion_connection',lambda _:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(native_stop_inventory,'workflow_processes',lambda *_:[])
    kb=SimpleNamespace(list_tasks=lambda _: [task],list_runs=lambda *_a,**_k:[ended])
    monkeypatch.setattr(native,'runtime',lambda:(kb,None,None))
    unblocked=[]
    def unblock(board,tid):
        assert (board,tid)==('retry-board','native-a')
        unblocked.append(tid)
        task.status='ready'
    monkeypatch.setattr(ingest_kanban,'KanbanCLI',lambda:SimpleNamespace(unblock=unblock))
    request={'run_id':run_id,'actor':'operator','operation_id':'retry-a','control_revision':5,'run_revision':7,'source_path':path,'source_sha256':raw_hash,'retry_kind':module.KIND}
    return SimpleNamespace(domain=domain,run=run,control=control,request=request,root=root,workspace=workspace,job=job,row=row,task=task,ended=ended,unblocked=unblocked)


def test_paused_retry_unblocks_same_task_without_releasing_boundary_or_reserving_calls(tmp_path,monkeypatch):
    item=fixture(tmp_path,monkeypatch)
    original=item.job.read_bytes()
    assert module.preview(tmp_path,item.request['run_id'],item.request['source_path'])['allowed']
    receipt=module.retry(tmp_path,item.request)
    assert item.unblocked==['native-a']
    assert receipt['task_ids']==['native-a'] and receipt['pause_released'] is False
    assert receipt['controller_requested'] is False and item.control['state']=='paused'
    assert item.control['boundary']=={'stage':'mapping'} and item.run['reservations']=={}
    assert item.job.read_bytes()==original
    assert module.retry(tmp_path,item.request)==receipt and item.unblocked==['native-a']


def test_active_retry_and_receipt_replay_attach_existing_controller(tmp_path, monkeypatch):
    import incremental_ingest_controller as controller

    item = fixture(tmp_path, monkeypatch)
    item.control.update(state='active', boundary=None)
    resumed = []
    monkeypatch.setattr(controller, 'attach_sources', lambda *_: resumed.append('attach'))
    monkeypatch.setattr(controller, 'spawn', lambda *_: resumed.append('start-or-reuse'))
    receipt = module.retry(tmp_path, item.request)
    assert receipt['controller_requested']
    assert module.retry(tmp_path, item.request) == receipt
    assert item.unblocked == ['native-a']
    assert resumed == ['attach', 'start-or-reuse', 'attach', 'start-or-reuse']


@pytest.mark.parametrize('evidence',['attempt','audit','result','reservation','claim','live-controller','changed-job','unsupported-raw','permission'])
def test_retry_refuses_consumption_live_ownership_or_failed_raw_source(tmp_path,monkeypatch,evidence):
    item=fixture(tmp_path,monkeypatch)
    if evidence in ('attempt','audit','result'):
        suffix={'attempt':'.attempt-1.json','audit':'.audit-1.json','result':'.result.json'}[evidence]
        _write_atomic(item.job.with_name(item.job.stem+suffix),_json_bytes({}))
    elif evidence=='reservation':
        item.run['reservations']['reserved']={'request_fingerprint':module.fingerprint({'request':item.row['id'],'ordinal':1})}
    elif evidence=='claim':item.task.claim_lock='live'
    elif evidence=='live-controller':monkeypatch.setattr(module,'controller_alive',lambda _:True)
    elif evidence=='changed-job':_write_atomic(item.job,_json_bytes({'request':{**item.row,'view':{'changed':True}}}))
    elif evidence=='permission':item.ended.error='PermissionError: access denied'
    else:item.run['sources'][item.request['source_path']]={'state':'failed','outcome':{'error_code':'CONVERSION_FAILED'}}
    capability=module.preview(tmp_path,item.request['run_id'],item.request['source_path'])
    assert capability['allowed'] is False
    with pytest.raises((ContractError,KeyError,ValueError)):
        module.retry(tmp_path,item.request)
    assert item.unblocked==[]


def test_prepared_retry_journal_recovers_exact_unclaimed_ready_prefix(tmp_path,monkeypatch):
    item=fixture(tmp_path,monkeypatch)
    data=module.inventory(item.domain,item.request['run_id'],item.request['source_path'])
    journal=item.root/'source-retries/retry-a.json'
    _write_atomic(journal,_json_bytes({'contract':'incremental-source-retry/v1','request':item.request,'state':'prepared','evidence':data}))
    item.task.status='ready'
    item.run['revision']+=1
    assert module.retry(tmp_path,item.request)['task_ids']==['native-a']
    assert json.loads(journal.read_text())['state']=='committed'


def test_second_operation_cannot_replenish_same_native_task_retry(tmp_path,monkeypatch):
    item=fixture(tmp_path,monkeypatch)
    module.retry(tmp_path,item.request)
    item.task.status='blocked'
    request={**item.request,'operation_id':'retry-again'}
    with pytest.raises(ContractError,match='already reserved'):
        module.retry(tmp_path,request)
    assert item.unblocked==['native-a']


def precall_fixture(tmp_path,monkeypatch):
    item=fixture(tmp_path,monkeypatch)
    import run_short_evidence_test as native
    item.ended.started_at=0
    item.ended.worker_pid=123
    item.ended.error=f'LOCK_BUSY at $: Skill kernel lock is held: {item.root / ".runtime-mutation.lock"}; wait limit=0s'
    item.attempt=item.job.with_name(item.job.stem+'.attempt-1.json')
    _write_atomic(item.attempt,_json_bytes({'ordinal':1,'request':item.row,'started_at':0.5,
        'grant':{'board':'retry-board','task_id':'native-a','run_id':item.ended.id}}))
    board=tmp_path/'host-kanban/retry-board'
    item.log=board/'logs/native-a.log'
    item.log.parent.mkdir(parents=True)
    item.log.write_text('Traceback in run_job\n reserve_call(root, original, ordinal, grant)\n'
        'Traceback in reserve_call\nTraceback in mutate\n'
        " with _exclusive_lock(domain.root(run_id) / '.runtime-mutation.lock'):\n"
        'hermes_source_units.validation.ContractError: '+item.ended.error+'\n')
    kb,_,_=native.runtime()
    kb.board_dir=lambda _:board
    kb.get_task=lambda *_:item.task
    return item


def test_attested_pre_reservation_failure_keeps_old_marker_and_adopts_ordinal_two(tmp_path,monkeypatch):
    item=precall_fixture(tmp_path,monkeypatch)
    original=item.attempt.read_bytes()
    data=module.inventory(item.domain,item.request['run_id'],item.request['source_path'])
    proof=data['targets'][0]['precall_attempt']
    assert proof['grant']['run_id']==3
    with pytest.raises(ContractError,match='committed precall'):
        module.retry_start_ordinal(item.workspace,item.row)
    module.retry(tmp_path,item.request)
    assert module.retry_start_ordinal(item.workspace,item.row)==2
    assert item.attempt.read_bytes()==original and item.run['reservations']=={}
    # Native logs append on later execution; the original attested prefix stays bound.
    with item.log.open('a') as stream:stream.write('later run log appended\n')
    assert module.retry_start_ordinal(item.workspace,item.row)==2


@pytest.mark.parametrize('change',['native-error','native-grant','worker-log','budget-reservation','budget-intent','budget-operation','transport'])
def test_pre_reservation_failure_requires_positive_native_chain_and_no_consumption(tmp_path,monkeypatch,change):
    item=precall_fixture(tmp_path,monkeypatch)
    key=module.fingerprint({'request':item.row['id'],'ordinal':1})
    if change=='native-error':item.ended.error='some unrelated failure'
    elif change=='native-grant':
        data=json.loads(item.attempt.read_text());data['grant']['run_id']=999
        _write_atomic(item.attempt,_json_bytes(data))
    elif change=='worker-log':item.log.write_text(item.ended.error+'\n')
    elif change=='budget-reservation':item.run['reservations']['first']={'request_fingerprint':key}
    elif change=='budget-intent':_write_atomic(item.root/'runtime-requests'/('call-'+key+'.json'),_json_bytes({}))
    elif change=='budget-operation':_write_atomic(item.root/'operations'/('call-'+key+'.json'),_json_bytes({}))
    else:_write_atomic(item.job.with_name(item.job.stem+'.transport.json'),_json_bytes({}))
    with pytest.raises(ContractError):module.retry(tmp_path,item.request)
    assert item.unblocked==[]


def test_pre_reservation_adoption_rejects_later_unknown_reserved_call(tmp_path,monkeypatch):
    item=precall_fixture(tmp_path,monkeypatch)
    module.retry(tmp_path,item.request)
    key=module.fingerprint({'request':item.row['id'],'ordinal':2})
    item.run['reservations']['later']={'request_fingerprint':key,'state':'reserved'}
    with pytest.raises(ContractError,match='later model call'):
        module.retry_start_ordinal(item.workspace,item.row)


def test_pre_reservation_adoption_rejects_changed_old_evidence(tmp_path,monkeypatch):
    item=precall_fixture(tmp_path,monkeypatch)
    module.retry(tmp_path,item.request)
    data=json.loads(item.attempt.read_text());data['started_at']=0.6
    _write_atomic(item.attempt,_json_bytes(data))
    with pytest.raises(ContractError,match='evidence changed'):
        module.retry_start_ordinal(item.workspace,item.row)
