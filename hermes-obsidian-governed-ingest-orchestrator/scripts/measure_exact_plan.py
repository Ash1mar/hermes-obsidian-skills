#!/usr/bin/env python3
"""Read-only new-plan parameter measurement; never creates a real plan or batch."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from hermes_source_units import FileIngestWorkflowService, ContractError
from hermes_source_units.exact_planning import ExactPlanner
from exact_model_budget import model_measurement


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vault',required=True);p.add_argument('--workflow-id',required=True)
    p.add_argument('--reader-limit',type=int,action='append')
    p.add_argument('--resource-id',action='append',help='Optional registered ready source scope')
    p.add_argument('--max-seconds',type=int,default=120)
    args=p.parse_args()
    workflow_service=FileIngestWorkflowService(args.vault)
    workflow=workflow_service.status(args.workflow_id)
    service=workflow_service.knowledge
    ready=[s for s in workflow['source_outcomes'] if s['status']=='ready']
    if args.resource_id:
        ready=[s for s in ready if s['resource_id'] in args.resource_id]
        if set(args.resource_id)!={s['resource_id'] for s in ready}:
            p.error('resource must belong to the selected workflow ready sources')
    if not ready:p.error('no ready registered sources in scope')
    registry=json.loads((service.vault/'_system/metadata/document-registry.json').read_text())['registry_revision']
    if args.max_seconds<1:p.error('max-seconds must be positive')
    started=time.monotonic();results=[]
    def check():
        if time.monotonic()-started>args.max_seconds:
            raise ContractError('MEASUREMENT_TIMEOUT','$','bounded read-only measurement stopped')
    for limit in args.reader_limit or [service._reader_config()['max_codepoints']]:
        for source in ready:
            planner=None;begin=time.monotonic()
            try:
                request={'actor':workflow['actor'],'registry_revision':registry,
                    'batch_id':'read-only-round3-measurement','max_codepoints':limit,
                    'sources':[{'resource_id':source['resource_id'],'unit_set_id':source['unit_set_id']}]}
                planner=ExactPlanner(service,request,check=check,read_only=True,
                    model_measure=model_measurement(service,workflow['actor'],registry))
                plan=planner.prepare()
                row={'state':'measured','units':len(planner.refs),'new_task_count':len(plan['tasks']),
                    'units_per_task':[len(t['target_refs']) for t in plan['tasks']],
                    'budget_policy':planner.budget_identity}
            except ContractError as exc:
                row={'state':'blocked','code':exc.code,'reason':str(exc),
                    'accepted_units':sum(len(t['target_refs']) for t in planner.tasks) if planner else 0}
            row.update(resource_id=source['resource_id'],reader_limit=limit,elapsed_seconds=time.monotonic()-begin)
            results.append(row)
            if row.get('code')=='MEASUREMENT_TIMEOUT':break
    print(json.dumps({'read_only':True,'real_model_calls':0,'logical_tasks_written':0,
        'existing_workflow_replanned':False,'results':results,'elapsed_seconds':time.monotonic()-started},ensure_ascii=False))


if __name__=='__main__':main()
