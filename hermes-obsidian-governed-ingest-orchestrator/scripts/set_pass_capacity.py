#!/usr/bin/env python3
"""Change only a stopped workflow's runtime Pass capacity, preserving slice inputs."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from ingest_kanban import IngestKanbanAdapter
from hermes_source_units.pass_capacity import set_capacity


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vault',required=True);p.add_argument('--workflow-id',required=True)
    p.add_argument('--limit',required=True,type=int);p.add_argument('--operation-id',required=True)
    a=p.parse_args()
    try:print(json.dumps(set_capacity(IngestKanbanAdapter(a.vault),a.workflow_id,a.limit,a.operation_id),ensure_ascii=False,indent=2))
    except Exception as exc:print(json.dumps({'ok':False,'error':str(exc)}),file=sys.stderr);return 2
    return 0


if __name__=='__main__':raise SystemExit(main())
