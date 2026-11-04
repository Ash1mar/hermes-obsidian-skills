"""Isolated handoff/watchdog contracts. No native workflow/model is started."""
import importlib.util,json,sys
from pathlib import Path
import pytest
from test_vault_run_policy import ROOT,start_and_pin
import watcher_runtime as runtime
import native_stream_policy as stream

def startup():
    spec=importlib.util.spec_from_file_location('startup_recovery_fixture',ROOT/'hermes-obsidian-governed-ingest-orchestrator/scripts/start_vault_workflow.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

@pytest.mark.parametrize('stop',[None,'cancelled','pause'])
def test_interrupted_start_hands_off_once_and_never_releases_stops(tmp_path,monkeypatch,stop):
    module=startup();calls=[]
    def dispatch(vault,command,request):
        calls.append(command)
        if command=='start':
            start_and_pin(vault,request)
            raise ModuleNotFoundError('isolated native import interruption')
        return {'background_dispatch':True,'reconciler_pid':123}
    monkeypatch.setattr(module,'dispatch',dispatch)
    monkeypatch.setattr(runtime,'coordinator_pid',lambda *_:None)
    observed=[];monkeypatch.setattr(runtime,'confirm_handoff',lambda *a:observed.append(a) or {'state':'native_coordinator_observed','pid':123})
    source=tmp_path/'source.md';source.write_text('mechanical fixture')
    request={'operation_id':'fixture','actor':'agent','vault_path':str(tmp_path/'new'),'vault_id':'fixture','vault_name':'Fixture','security_domain':'test','sources':[str(source)]}
    record=tmp_path/'receipt.json';bootstrap=ROOT/'hermes-obsidian-vault-bootstrap'
    with pytest.raises(ModuleNotFoundError):module.start(request,record,bootstrap)
    if stop:
        monkeypatch.setattr('hermes_source_units.FileIngestWorkflowService.pending_pause',lambda *_:stop=='pause')
        if stop=='cancelled':
            from hermes_source_units import FileIngestWorkflowService,mutation_digest
            s=FileIngestWorkflowService(request['vault_path']);w=s.status('ingest-fixture')
            req={'workflow_id':w['workflow_id'],'actor':w['actor'],'expected_revision':w['revision']};req['input_digest']=mutation_digest(req);s.cancel(req)
    module.start(request,record,bootstrap);module.start(request,record,bootstrap)
    assert calls==(['start'] if stop else ['start','resume'])
    assert len(observed)==(0 if stop else 1)

def test_start_cli_selects_native_interpreter_before_parsing_or_writing(monkeypatch):
    module=startup();seen=[];monkeypatch.setattr(runtime,'ensure_native_runtime',lambda:seen.append('native'))
    monkeypatch.setattr(sys,'argv',['start_vault_workflow.py','--help'])
    with pytest.raises(SystemExit) as e:module.main()
    assert e.value.code==0 and seen==['native']

@pytest.mark.parametrize('live,marker',[(True,True),(False,True),(True,False)])
def test_handoff_requires_actual_owner_and_matching_marker(tmp_path,monkeypatch,live,marker):
    monkeypatch.setattr('hermes_source_units.runtime_telemetry.runtime_directory',lambda *_:tmp_path)
    monkeypatch.setattr(runtime,'coordinator_pid',lambda *_:7 if live else None)
    (tmp_path/'watcher.json').write_text(json.dumps({'workflow_id':'w','pid':7 if marker else 99}))
    if live and marker:assert runtime.confirm_handoff(tmp_path,'w',7,timeout=0)['pid']==7
    else:
        with pytest.raises(RuntimeError,match='ownership'):runtime.confirm_handoff(tmp_path,'w',7,timeout=0)

@pytest.mark.parametrize('value',[None,'45','0','bad','nan'])
def test_fixed_stream_default_preserves_explicit_host_policy(monkeypatch,value):
    monkeypatch.setattr('orchestration.configuration',lambda:{'native_stream_idle_seconds':120})
    if value is None:monkeypatch.delenv(stream.IDLE_ENV,raising=False)
    else:monkeypatch.setenv(stream.IDLE_ENV,value)
    if value in ('bad','nan'):
        with pytest.raises(ValueError):stream.configure()
    else:
        result=stream.configure();assert result['idle_seconds']==(120 if value is None else float(value))
        assert result['retries_granted']==0 and result['total_budget_seconds']==600
