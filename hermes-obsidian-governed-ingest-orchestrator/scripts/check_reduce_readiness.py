#!/usr/bin/env python3
"""Inventory every task in stopped Reduce without invoking models or resuming."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from ingest_kanban import IngestKanbanAdapter
from reduce_readiness import scan

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ('vault','workflow-id','output'):p.add_argument('--'+flag,required=True)
    args=p.parse_args()
    report=scan(IngestKanbanAdapter(args.vault),args.workflow_id)
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():raise SystemExit('Choose a fresh output path; readiness evidence is not overwritten')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'report_id':report['report_id'],'counts':report['counts'],
        'sources':[{k:s[k] for k in ('resource_id','disposition','budget')} for s in report['sources']],
        'global_budget':report['global_budget'],'native_model_calls':0,'resumed':False},ensure_ascii=False))
