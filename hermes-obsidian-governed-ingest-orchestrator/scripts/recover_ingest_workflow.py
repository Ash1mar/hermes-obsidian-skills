#!/usr/bin/env python3
"""Program-native finite Pass recovery; unsafe or unproven failures remain held."""
import argparse,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from ingest_kanban import IngestKanbanAdapter
from automatic_pass_recovery import recover
from native_stop_inventory import workflow_processes


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vault',required=True);p.add_argument('--workflow-id',required=True)
    args=p.parse_args();adapter=IngestKanbanAdapter(args.vault,enable_workers=True)
    deadline=time.monotonic()+40
    while True:
        w=adapter.workflow.status(args.workflow_id)
        live=workflow_processes(args.workflow_id,w['kanban']['board_id'])
        if not live:break
        if time.monotonic()>=deadline:raise RuntimeError('automatic recovery remains held: native consumers have not drained')
        time.sleep(.2)
    result=recover(adapter,args.workflow_id)
    print(json.dumps({'automatic_recovery_applied':result,'operator_issue_selection_required':False}),flush=True)


if __name__=='__main__':main()
