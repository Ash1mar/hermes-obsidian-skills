#!/usr/bin/env python3
"""Inspect/apply explicit stopped-workflow Finalize disposition controls."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from ingest_kanban import IngestKanbanAdapter
from hermes_source_units import ContractError
from finalize_disposition_control import apply

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vault',required=True);p.add_argument('--request',required=True);p.add_argument('--dry-run',action='store_true')
    args=p.parse_args()
    try:
        request=json.loads(Path(args.request).read_text(encoding='utf-8-sig'))
        result=apply(IngestKanbanAdapter(args.vault),request,dry_run=args.dry_run)
    except (ContractError,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({'ok':False,'code':getattr(exc,'code','INVALID_REQUEST'),'error':str(exc)},ensure_ascii=False));return 2
    print(json.dumps(result,ensure_ascii=False));return 0

if __name__=='__main__':raise SystemExit(main())
