"""Mechanical projections and hints keep native/domain authority intact."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest
from test_compact_execution import stopped, vault
from ingest_kanban import KanbanCLI
from hermes_source_units import runtime_telemetry as telemetry


def test_projection_reads_listing_once_and_expires_at_exit():
    calls=[]
    rows=[{'id':f't{i}','status':'blocked','started_at':None} for i in range(60)]
    def invoke(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv,0,json.dumps(rows),'')
    cli=KanbanCLI(invoke)
    with cli.projection('board'):
        for row in rows:
            cli.wait('board',row['id'],'blocked','already held')
            assert not cli.task_has_runs('board',row['id'])
    assert len(calls)==1
    rows[0]['started_at']=1
    with cli.projection('board'):
        assert cli.task_has_runs('board','t0')
    assert len(calls)==2


def test_projection_refreshes_parent_promotions_and_caches_attempts():
    calls=[]
    rows={'parent':{'id':'parent','status':'running','started_at':1},
          'child':{'id':'child','status':'blocked','started_at':None}}
    def invoke(argv):
        verb=argv[4];calls.append(verb)
        if verb=='list':value=list(rows.values())
        elif verb=='show':value={'task':dict(rows[argv[5]]),'runs':[]}
        else:
            value={}
            if verb=='complete':
                rows['parent']['status']='done';rows['child']['status']='ready'
            if verb=='block':rows[argv[5]]['status']='blocked'
        return subprocess.CompletedProcess(argv,0,json.dumps(value),'')
    cli=KanbanCLI(invoke)
    with cli.projection('board'):
        cli.task_snapshot('board','parent');cli.task_snapshot('board','parent')
        cli.complete('board','parent','durable outcome')
        assert cli.task_status('board','child')=='ready'
        cli.wait('board','child','blocked','durable pause')
        assert cli.task_status('board','child')=='blocked'
    assert calls.count('show')==1 and calls.count('list')==3 and 'block' in calls


def test_unknown_attempt_metadata_is_not_treated_as_never_started():
    cli=KanbanCLI(lambda args:subprocess.CompletedProcess(args,0,
        json.dumps([{'id':'t','status':'ready'}]),''))
    with cli.projection('board'):
        assert cli.task_has_runs('board','t')


def test_projection_includes_archived_status_without_per_card_reads():
    calls=[];rows=[{'id':f'old-{i}','status':'archived','started_at':1} for i in range(40)]
    def invoke(argv):
        calls.append(argv)
        assert argv[4]=='list' and '--archived' in argv
        return subprocess.CompletedProcess(argv,0,json.dumps(rows),'')
    cli=KanbanCLI(invoke)
    with cli.projection('board'):
        cli.ensure_board('board')
        for row in rows:
            assert cli.task_status('board',row['id'])=='archived'
            cli.archive('board',row['id'])
    assert len(calls)==1


def test_archived_status_reuses_same_projection_show_and_expires():
    calls=[]
    def invoke(argv):
        verb=argv[4];calls.append(verb)
        return subprocess.CompletedProcess(argv,0,json.dumps([] if verb=='list' else
            {'task':{'id':'old','status':'archived'},'runs':[],'children':[]}),'')
    cli=KanbanCLI(invoke)
    with cli.projection('board'):
        cli.task_snapshot('board','old')
        for _ in range(3):
            assert cli.task_status('board','old')=='archived'
            cli.archive('board','old')
        assert calls.count('show')==1
        cli.task_snapshot_fresh('board','old')
        assert calls.count('show')==2
    cli.task_status('board','old')
    assert calls.count('show')==3


def test_nested_timeline_and_failed_diagnostic_do_not_change_execution(tmp_path, monkeypatch):
    root=tmp_path/'timeline'
    monkeypatch.setattr(telemetry,'runtime_directory',lambda *args:root)
    with telemetry.span('parent',vault=tmp_path,workflow_id='ingest-test') as parent:
        with telemetry.span('child',task_id='t'):
            pass
    rows=[json.loads(s) for s in (root/f'{os.getpid()}.jsonl').read_text().splitlines()]
    assert rows[0]['parent_span_id']==parent['span_id']
    assert rows[0]['elapsed_ms']<=rows[1]['elapsed_ms']
    monkeypatch.setattr(telemetry,'runtime_directory',lambda *args:root/'missing'/'bad')
    monkeypatch.setattr(Path,'mkdir',lambda *args,**kwargs:(_ for _ in ()).throw(OSError('isolated diagnostics failure')))
    with telemetry.span('failed-log',vault=tmp_path,workflow_id='ingest-test'):
        result=42
    assert result==42


def test_wake_hint_cannot_signal_an_unrelated_process(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry,'runtime_directory',lambda *args:tmp_path)
    (tmp_path/'watcher.json').write_text(json.dumps({'pid':os.getpid(),'workflow_id':'ingest-other'}))
    monkeypatch.setattr(os,'kill',lambda *args:pytest.fail('unrelated process must not be signalled'))
    assert not telemetry.notify_reconciler(tmp_path,'ingest-test')


@pytest.mark.skipif(os.name!='posix',reason='native watcher uses Linux signals')
def test_watcher_wakes_and_removes_only_its_own_hint(tmp_path, monkeypatch):
    import importlib.util
    from test_p3_knowledge_build import ROOT
    script=ROOT/'hermes-obsidian-governed-ingest-orchestrator/scripts/watch_ingest_workflow.py'
    spec=importlib.util.spec_from_file_location('isolated_watcher',script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'runtime_directory',lambda *args:tmp_path)
    previous=signal.getsignal(signal.SIGUSR1)
    with module.wake_events(tmp_path,'ingest-test') as event:
        os.kill(os.getpid(),signal.SIGUSR1)
        assert event.wait(.5)
        assert json.loads((tmp_path/'watcher.json').read_text())['pid']==os.getpid()
    assert not (tmp_path/'watcher.json').exists()
    assert signal.getsignal(signal.SIGUSR1)==previous


def test_native_capacity_snapshot_counts_claims_and_leases_once(monkeypatch, tmp_path):
    import native_program_dispatch as dispatcher
    leased=SimpleNamespace(_batch=lambda _: {'slice_config':{'pass_worker_concurrency':2}},
        _slices=lambda _: [{'slice_id':'s1','lease':{'worker_id':'w1'}}])
    cards=[{'task_id':f't{i}','node':f'pass-slice:s{i}'} for i in range(1,5)]
    value={'cancel_requested':False,'batch_id':'batch','kanban':{'board_id':'board','task_map':cards}}
    adapter=SimpleNamespace(program_worker_concurrency=2,workflow=SimpleNamespace(vault=tmp_path,status=lambda _:value,
        pending_pause=lambda _:False,knowledge=leased))
    adapter.reconcile_pass_workers=lambda _:None
    tasks=[SimpleNamespace(id=f't{i}',status='running' if i<=2 else 'ready',assignee='ingest-program') for i in range(1,5)]
    reads=[]
    kb=SimpleNamespace(list_tasks=lambda _:reads.append('list') or tasks,
        get_task=lambda *args:pytest.fail('full capacity must not inspect or claim candidates'))
    kd=SimpleNamespace(reap_worker_zombies=lambda:None,configured_max_in_progress=lambda:None,
        count_running_tasks=lambda _:2,count_running_tasks_other_boards=lambda _:0,
        resolve_max_in_progress=lambda _:None,_memory_pressure_level=lambda:'normal')
    class Connection:
        def close(self):pass
    hermes=ModuleType('hermes_cli');hermes.kanban_db=kb;hermes.kanban_db_connect=SimpleNamespace(connect=lambda **kw:Connection())
    hermes.kanban_db_dispatch=kd;hermes.kanban_db_workspace=SimpleNamespace()
    profiles=ModuleType('hermes_cli.profiles');profiles.profile_exists=lambda _:False
    monkeypatch.setitem(sys.modules,'hermes_cli',hermes);monkeypatch.setitem(sys.modules,'hermes_cli.profiles',profiles)
    assert dispatcher.dispatch_programs(adapter,'ingest-test')==[]
    assert reads==['list']
