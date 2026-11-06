#!/usr/bin/env python3
"""Default test rerun entry: one existing workflow, one pinned node boundary."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from watcher_runtime import ensure_native_runtime
from ingest_kanban import IngestKanbanAdapter
from orchestration import _mutation,configuration
from hermes_source_units.source_units import _write_atomic,_json_bytes
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_test import CONTRACT,NODES,root
from workflow_test_entry import describe,reenter,handoff

ALIASES={'mineru':'source-prepare','post-mineru':'exact-plan','reduce':'resource-reduce','map':'pass'}


def main():
    ensure_native_runtime()
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vault',required=True,type=Path)
    p.add_argument('--request',type=Path)
    p.add_argument('--record',type=Path)
    p.add_argument('--workflow-id')
    p.add_argument('--operation-id')
    p.add_argument('--from-node',choices=(*NODES,*ALIASES))
    p.add_argument('--pass-capacity',type=int)
    p.add_argument('--plan',action='store_true')
    p.add_argument('--list',action='store_true')
    p.add_argument('--dry-run',action='store_true')
    a=p.parse_args()
    try:
        adapter=IngestKanbanAdapter(a.vault)
        if a.list:
            if not a.workflow_id:raise ValueError('list requires workflow ID')
            adapter.workflow.status(a.workflow_id)
            path=root(adapter.workflow.vault,a.workflow_id)/'index.json'
            index=json.loads(path.read_text()) if path.exists() else {}
            print(json.dumps({'workflow_id':a.workflow_id,'default_reuse':'same_workflow','aliases':ALIASES,
                'nodes':[{'node':n,'boundary_available':n in index,'boundary':index.get(n),
                          'historical_reduce_probe':n=='resource-reduce' and n not in index} for n in NODES],
                'model_calls':0,'resumed':False},ensure_ascii=False,indent=2))
            return 0
        if not a.request:raise ValueError('pinned request path required')
        if a.plan:
            if not a.workflow_id or not a.operation_id or not a.from_node:
                raise ValueError('plan requires workflow, stable operation and from-node')
            node=ALIASES.get(a.from_node,a.from_node)
            w=adapter.workflow.status(a.workflow_id)
            path=root(adapter.workflow.vault,a.workflow_id)/'index.json'
            index=json.loads(path.read_text()) if path.exists() else {}
            if node=='resource-reduce' and node not in index:
                from reduce_test import CONTRACT as LEGACY,describe as legacy_describe
                prefix=legacy_describe(adapter.workflow.vault,a.workflow_id)
                request=_mutation(w,test_mode=LEGACY,operation_id=a.operation_id,from_node=node,
                    overwrite_downstream=True,prefix_sha256=fingerprint(prefix))
            else:
                data=describe(adapter,a.workflow_id,node)
                capacity=configuration()['pass_worker_concurrency'] if a.pass_capacity is None else a.pass_capacity
                if type(capacity) is not int or not 1<=capacity<=10:raise ValueError('Pass capacity must be 1 through 10')
                request=_mutation(w,test_mode=CONTRACT,operation_id=a.operation_id,from_node=node,
                    overwrite_downstream=True,boundary_sha256=data['boundary_sha256'],pass_capacity=capacity)
            if a.request.exists() and json.loads(a.request.read_text())!=request:
                raise ValueError('pinned request exists with different inputs')
            if not a.request.exists():_write_atomic(a.request,_json_bytes(request))
            print(json.dumps({'ok':True,'request':str(a.request),'workflow_id':a.workflow_id,
                'from_node':node,'mode':request['test_mode'],'model_calls':0,'resumed':False}))
            return 0
        request=json.loads(a.request.read_text(encoding='utf-8-sig'))
        if not a.dry_run and not a.record:raise ValueError('external receipt path required before admission')
        if a.from_node and ALIASES.get(a.from_node,a.from_node)!=request.get('from_node','resource-reduce'):
            raise ValueError('requested node differs from pinned request')
        from reduce_test import CONTRACT as LEGACY,reenter as legacy_reenter
        if request.get('test_mode')==LEGACY:result=legacy_reenter(adapter,request,dry_run=a.dry_run)
        elif request.get('test_mode')==CONTRACT:result=reenter(adapter,request,dry_run=a.dry_run)
        else:raise ValueError('explicit same-workflow test request required')
        if not a.dry_run:
            if not a.record:raise ValueError('external receipt path required')
            result=handoff(adapter,request,result)
            _write_atomic(a.record,_json_bytes(result))
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
