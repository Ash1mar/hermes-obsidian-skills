#!/usr/bin/env python3
"""One bounded semantic child; the native program retains card ownership."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from fixed_semantic import HermesCaller
from fixed_semantic_schema import schema


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    request=json.loads(Path(args.request).read_text())
    if (os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')!='1'
            or os.environ.get('HERMES_INGEST_EXECUTOR')!='semantic-v1'
            or request['native_task_id']!=os.environ.get('HERMES_KANBAN_TASK')
            or request['native_run_id']!=os.environ.get('HERMES_KANBAN_RUN_ID')
            or request['schema']!=schema(request['phase'])):
        raise RuntimeError('fixed semantic child lacks its native parent grant')
    caller=HermesCaller();caller.input_limit=request['input_limit']
    raw,metadata=caller(request['phase'],request['view'],request['schema'],request['correction'])
    metadata.update(call_ownership='semantic_child',parent_native_run_id=request['native_run_id'])
    Path(args.output).write_text(json.dumps({'response':raw,'metadata':metadata},ensure_ascii=False))


if __name__=='__main__':
    main()
