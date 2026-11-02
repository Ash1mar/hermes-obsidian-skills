#!/usr/bin/env python3
"""Recover exact cancelled pause control without dispatch or domain calls."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from ingest_kanban import IngestKanbanAdapter
from pause_control_recovery import recover
from hermes_source_units.validation import record_validation_scope
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vault',required=True);p.add_argument('--request',required=True);p.add_argument('--dry-run',action='store_true');a=p.parse_args()
    adapter=IngestKanbanAdapter(a.vault)
    with record_validation_scope(),adapter.workflow.read_projection():
        result=recover(adapter,json.loads(Path(a.request).read_text(encoding='utf-8-sig')),dry_run=a.dry_run)
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
