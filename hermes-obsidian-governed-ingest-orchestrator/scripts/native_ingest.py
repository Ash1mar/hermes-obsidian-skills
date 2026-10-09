#!/usr/bin/env python3
"""JSON-stdin native bridge; no model-authored shell or domain semantics."""
import argparse
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root/'hermes-obsidian-controlled-ingest/lib'))
sys.path.insert(0, str(root/'hermes-obsidian-governed-ingest-orchestrator/lib'))
from hermes_source_units import ContractError
from native_ingest import control_workflow, worker_action, workflow_status


def main():
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', required=True, choices=('begin','read','submit','confirm','heartbeat','complete','status','control'))
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin)
        vault = request['vault']
        if args.action=='status':
            result = workflow_status(vault, request.get('workflow_id'), runtime=request.get('runtime',False))
        elif args.action=='control':
            result = control_workflow(vault, request['workflow_id'], request['action'], request['operation_id'])
        else:
            result = worker_action(vault, args.action, {k:v for k,v in request.items() if k!='vault'})
        content = result.pop('content', None)
        print(json.dumps(result, ensure_ascii=False))
        if content is not None:
            print(content, end='')
        return 0
    except (ContractError,OSError,ValueError,TypeError,KeyError) as exc:
        result = {'ok':False,'error':str(exc)}
        if isinstance(exc, ContractError):
            result.update(code=exc.code,path=exc.path)
        print(json.dumps(result,ensure_ascii=False))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
