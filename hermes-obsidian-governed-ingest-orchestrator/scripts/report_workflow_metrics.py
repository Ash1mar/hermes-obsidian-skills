#!/usr/bin/env python3
"""Aggregate existing workflow evidence without executing or mutating the Vault."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from hermes_source_units import FileIngestWorkflowService
from workflow_metrics import collect


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vault',required=True,type=Path);p.add_argument('--workflow-id',required=True)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--native-db',type=Path);p.add_argument('--windows',type=Path)
    p.add_argument('--protected-baseline',type=Path)
    args=p.parse_args();dest=args.output.resolve();vault=args.vault.resolve()
    if dest.is_relative_to(vault) or args.output.is_symlink() or dest.exists():
        p.error('write a new external report; do not overwrite Vault or historical evidence')
    result=collect(FileIngestWorkflowService(vault),args.workflow_id,database=args.native_db,
        windows=json.loads(args.windows.read_text(encoding='utf-8')) if args.windows else (),
        protected=json.loads(args.protected_baseline.read_text(encoding='utf-8')) if args.protected_baseline else None)
    dest.parent.mkdir(parents=True,exist_ok=True)
    with dest.open('x',encoding='utf-8') as stream:stream.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'output':str(dest),'snapshot_consistent':result['snapshot_consistent'],
                     'current_snapshot':result['current_snapshot']},ensure_ascii=False))


if __name__=='__main__':main()
