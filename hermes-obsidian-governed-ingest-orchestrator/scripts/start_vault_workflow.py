#!/usr/bin/env python3
"""Trusted short-prompt entry: bootstrap, import originals, start one workflow."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from hermes_source_units.source_units import _exclusive_lock, _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint
from hermes_source_units.runtime_policy import POLICY_REF
from orchestration import configuration, dispatch
from hermes_source_units.runtime_telemetry import measured, event


@measured('vault_startup',lambda request,record,bootstrap_skill:{'vault':request.get('vault_path'),
    'workflow_id':'ingest-'+str(request.get('operation_id','unknown')),'operation_id':request.get('operation_id')})
def start(request, record, bootstrap_skill):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise ValueError('startup requires a trusted operator')
    required={'operation_id','actor','vault_path','vault_id','vault_name','security_domain','sources'}
    if not required <= set(request) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,70}',request['operation_id']):
        raise ValueError('missing startup fields or invalid operation_id')
    vault=Path(request['vault_path'])
    if not vault.is_absolute() or vault.is_symlink(): raise ValueError('explicit absolute Vault required')
    vault=vault.resolve(); record=Path(record).absolute()
    if record.is_symlink(): raise ValueError('startup receipt cannot be a symlink')
    sources=[Path(p).resolve(strict=True) for p in request['sources']]
    if not sources or any(not p.is_file() for p in sources) or len({p.name for p in sources})!=len(sources):
        raise ValueError('unique source basenames required')
    cfg=configuration()
    policy={k:cfg[k] for k in ('source_prepare_concurrency','program_worker_concurrency','reduce_page_concurrency','pass_worker_concurrency','reduce_worker_concurrency')}
    policy.update(request.get('execution_policy',{}))
    from hermes_source_units.recovery_policy import DEFAULT, validate as validate_recovery
    recovery=validate_recovery(request.get('recovery_policy',{
        **DEFAULT,'semantic_failure':cfg.get('pass_failure_policy','hold')}))
    from hermes_source_units.validation import validate_record
    validate_record('ingest_workflow_scope',{'source_paths':[],'knowledge_selector':'startup',
        'execution_policy':policy,'canary_policy':request.get('canary_policy','sample')})
    if request.get('canary_policy')=='off' and 'canary' in request.get('pause_after',[]):
        raise ValueError('Canary off cannot request a canary pause')
    identity={'request':request,'source_hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}
    digest=fingerprint(identity)
    record.parent.mkdir(parents=True,exist_ok=True)
    with _exclusive_lock(record.with_suffix('.lock')):
        state=json.loads(record.read_text()) if record.exists() else None
        if state and state['input_digest']!=digest: raise ValueError('startup identity or source bytes changed')
        if state is None:
            if vault.exists() and any(vault.iterdir()): raise ValueError('new Vault target is nonempty')
            state={'contract':'vault-startup/v1','input_digest':digest,'operation_id':request['operation_id'],
                   'vault':str(vault),'execution_policy':policy,'recovery_policy':recovery,'identity':identity,'phase':'bootstrap'}
            _write_atomic(record,_json_bytes(state))
        def save(): _write_atomic(record,_json_bytes(state))
        if state['phase']=='bootstrap':
            # A completed scaffold can be authenticated after an interrupted handoff.
            marker=vault/'_system/vault.json'
            if not marker.exists():
                result=subprocess.run([sys.executable,str(Path(bootstrap_skill)/'scripts/init_obsidian_vault.py'),
                    '--vault-path',str(vault),'--profile','engineering','--vault-id',request['vault_id'],
                    '--vault-name',request['vault_name'],'--security-domain',request['security_domain']],capture_output=True,text=True)
                state['bootstrap_returncode']=result.returncode; state['bootstrap_output']=result.stdout; save()
                if result.returncode: raise RuntimeError(result.stderr or result.stdout)
            actual=json.loads(marker.read_text())['vault']
            if any(actual[k]!=request['vault_'+k] for k in ('id','name')) or actual['security_domain']!=request['security_domain']:
                raise ValueError('bootstrap identity mismatch')
            subprocess.run([sys.executable,str(Path(bootstrap_skill)/'scripts/validate_source_unit_vault.py'),'--vault',str(vault)],check=True,capture_output=True)
            state['phase']='import'; save()
            event('bootstrap_completed',vault_id=request['vault_id'],bootstrap_record=str(record))
        paths=[]
        for source in sources:
            relative='10_Raw/'+source.name; target=vault/relative; data=source.read_bytes()
            if not target.resolve().is_relative_to(vault): raise ValueError('original target escapes Vault')
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.is_symlink() or (target.exists() and target.read_bytes()!=data): raise ValueError('original target conflict')
            if not target.exists():
                with target.open('xb') as out: out.write(data)
            paths.append(relative)
        provider=request.get('retrieval_policy',{'contract':'vault-retrieval-policy/v1','sync_enabled':False,'query_enabled':False})
        from hermes_source_units.runtime_policy import vault_provider_config
        pp=vault/POLICY_REF
        if pp.exists() and json.loads(pp.read_text())!=provider: raise ValueError('Provider policy conflict')
        if not pp.exists(): _write_atomic(pp,_json_bytes(provider))
        vault_provider_config({},vault,'sync')
        ref='_system/reports/vault-startup/'+request['operation_id']+'.json'
        scope={'source_paths':paths,'knowledge_selector':'Complete the scoped sources through Final Acceptance using native workers.',
            'execution_mode':'auto_full','pause_after':request.get('pause_after',[]),'canary_policy':request.get('canary_policy','sample'),
            'provider':'sync' if provider['sync_enabled'] else 'skip','execution_policy':state['execution_policy'],
            'provider_policy_sha256':hashlib.sha256(pp.read_bytes()).hexdigest(),'bootstrap_ref':ref}
        workflow={'workflow_id':'ingest-'+request['operation_id'],'actor':request['actor'],'profile':'compact-5',
                  'scope':scope,'expected_revision':0}
        # Old startup receipts retain their original scope/digest on replay.
        if 'recovery_policy' in state:scope['recovery_policy']=state['recovery_policy']
        workflow['input_digest']='sha256:'+fingerprint(workflow)
        # Stable report bytes become workflow evidence; mutable handoff status stays external.
        report={'contract':'vault-startup-report/v1','input_digest':digest,'vault_id':request['vault_id'],
            'workflow_id':workflow['workflow_id'],'bootstrap_output':state.get('bootstrap_output',''),
            'source_hashes':identity['source_hashes'],'execution_policy':state['execution_policy']}
        if 'recovery_policy' in state:report['recovery_policy']=state['recovery_policy']
        rp=vault/ref
        if rp.exists() and json.loads(rp.read_text())!=report: raise ValueError('startup report conflict')
        if not rp.exists(): _write_atomic(rp,_json_bytes(report))
        from hermes_source_units import FileIngestWorkflowService
        service=FileIngestWorkflowService(vault)
        # Once a workflow exists, retries observe it; resume is a distinct operation.
        existing=service._path(workflow['workflow_id'])
        handoff=False
        if existing.exists():
            value=service.status(workflow['workflow_id'])
            if value['start_digest']!=workflow['input_digest']: raise ValueError('workflow request conflict')
            if not value.get('template_pins'):
                from orchestration import start_and_pin
                start_and_pin(vault,workflow)
            result=service.status(workflow['workflow_id'],compact=True)
            if state['phase']!='workflow_created' and value['state'] not in ('cancelled','completed','partial','failed') and not service.pending_pause(value):
                from orchestration import _mutation
                from watcher_runtime import coordinator_pid
                pid=coordinator_pid(vault,workflow['workflow_id'])
                result=({**result,'reconciler_pid':pid} if pid else
                    dispatch(vault,'resume',_mutation(service.status(workflow['workflow_id']))))
                handoff=True
        else:
            result=dispatch(vault,'start',workflow)
            handoff=True
        if handoff and (result.get('background_dispatch') or result.get('initial_release_pending')):
            from watcher_runtime import confirm_handoff
            state['handoff']=confirm_handoff(vault,workflow['workflow_id'],result.get('reconciler_pid'))
        state.update(phase='workflow_created',workflow_id=workflow['workflow_id'],bootstrap_ref=ref); save()
        event('startup_workflow_link',vault_id=request['vault_id'],bootstrap_ref=ref,
              execution_policy=state['execution_policy'])
        return {'startup':state,'workflow':result}


def main():
    from watcher_runtime import ensure_native_runtime
    ensure_native_runtime()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',required=True,type=Path)
    parser.add_argument('--record',required=True,type=Path)
    parser.add_argument('--bootstrap-skill',required=True,type=Path)
    args=parser.parse_args()
    try:
        print(json.dumps(start(json.loads(args.request.read_text(encoding='utf-8-sig')),args.record,args.bootstrap_skill),ensure_ascii=False,indent=2))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False),file=sys.stderr); return 2
    return 0


if __name__=='__main__': raise SystemExit(main())
