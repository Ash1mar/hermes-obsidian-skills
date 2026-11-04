#!/usr/bin/env python3
"""Supervise finite workflow recovery; unknown domain completion remains held."""
import argparse,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from ingest_kanban import IngestKanbanAdapter
from recovery_controller import prepare,attempt,supervise
from hermes_source_units.source_units import _load_json,_json_bytes,_write_atomic,_exclusive_lock


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vault',required=True);p.add_argument('--workflow-id',required=True)
    p.add_argument('--episode');p.add_argument('--attempt-output',type=Path)
    args=p.parse_args();adapter=IngestKanbanAdapter(args.vault,enable_workers=True)
    key=args.episode or prepare(adapter,args.workflow_id)
    if len(key)!=24 or any(c not in '0123456789abcdef' for c in key):raise ValueError('invalid episode')
    if args.attempt_output:
        root=adapter.workflow.vault/f'_system/ledgers/ingest-workflows/{args.workflow_id}/automatic-control'
        if args.attempt_output.resolve().parent!=root.resolve() or args.attempt_output.exists():raise ValueError('invalid attempt receipt')
        record=_load_json(root/(key+'.json'))
        with _exclusive_lock(root/'.attempt.lock'):
            try:result={'ok':True,'applied':attempt(adapter,args.workflow_id,record)}
            except Exception as exc:result={'ok':False,'code':getattr(exc,'code',type(exc).__name__),'error':str(exc)}
            _write_atomic(args.attempt_output,_json_bytes(result))
    else:result=supervise(adapter,args.workflow_id,key,Path(__file__))
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
