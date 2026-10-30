#!/usr/bin/env python3
"""Authorize an exact delivery with exclusions while the Build workflow is stopped."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from ingest_kanban import IngestKanbanAdapter
from finalize_delivery_control import authorize
from hermes_source_units.validation import record_validation_scope

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vault',required=True)
    p.add_argument('--request',required=True);p.add_argument('--dry-run',action='store_true');a=p.parse_args()
    adapter=IngestKanbanAdapter(a.vault)
    request=json.loads(Path(a.request).read_text(encoding='utf-8-sig'))
    with record_validation_scope(),adapter.workflow.read_projection():
        action=authorize
        if request.get('action')=='authorize-program-repairs':
            from finalize_program_repair import authorize as action
        if request.get('action')=='run-delivery':
            from finalize_delivery_flow import run as action
        result=action(adapter,request,dry_run=a.dry_run)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
