#!/usr/bin/env python3
"""Prepare evidence-bound source-packet recovery and targeted dependency revision."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from ingest_kanban import IngestKanbanAdapter
from native_stop_inventory import verify_cancel_drain
from hermes_source_units import ContractError,mutation_digest
from hermes_source_units.source_units import _load_json,_vault_path,_json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint,record_validation_scope
from hermes_source_units.reading_dependencies import citation_dependency_gaps
from hermes_source_units.knowledge_build import _covers
from fixed_reduce_packets import contract_hash


def prepare(vault,wid,rid,output):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise ContractError('ACCESS_DENIED','$','repair preparation requires a trusted operator')
    if not rid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in rid) or len(rid)>64:
        raise ContractError('INVALID_SCHEMA','$','invalid repair ID')
    a=IngestKanbanAdapter(vault);w=a.workflow.status(wid);k=a.workflow.knowledge
    if w['state']!='cancelled' or w['current_stage']!='reducing':
        raise ContractError('INVALID_TRANSITION','$','requires cancelled Reduce')
    verify_cancel_drain(a,wid,timeout=0);a.workflow.assert_execution_ready(w)
    failures=[];budget=[];resources=set()
    for card in w['kanban']['task_map']:
        if not card['node'].startswith('resource-reduce:'):continue
        ref=f"_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(card['node'])[:16]}.json"
        path=_vault_path(a.workflow.vault,ref)
        if not path.is_file():continue
        data=path.read_bytes();failure=json.loads(data)
        if failure.get('task_id')!=card['task_id'] or failure.get('idempotency_key')!=card['idempotency_key']:continue
        entry={'node':card['node'],'task_id':card['task_id'],'failure_sha256':hashlib.sha256(data).hexdigest()}
        if failure.get('error_code')=='READING_WINDOW_OVERSIZE':budget.append(entry)
        if failure.get('error_code')=='INCOMPLETE_COVERAGE' and 'required original dependency lacks citation inspection' in failure.get('reason',''):
            failures.append(entry);resources.add(card['node'].partition(':')[2])
    batch=k._batch(w['batch_id']);skipped=a.workflow.skipped_pass_tasks(w)
    tasks=[k._task(t) for t in batch['task_ids'] if t not in skipped]
    candidates,by_task=k._pass_index(t['task_id'] for t in tasks)
    selected={r['pass_id']:r for r,c in candidates.values() if r['pass_kind']=='citation'}
    gaps=[];items=[]
    for record in selected.values():
        task=next(t for t in tasks if t['task_id']==record['task_id'])
        if not {r['unit_ref']['resource_id'] for r in task['target_refs']}.issubset(resources):continue
        package=_load_json(_vault_path(a.workflow.vault,
            f"_system/knowledge-builds/task-{task['task_id']}/readings/{record['reading_package_id']}.json"))
        missing=citation_dependency_gaps(k.source,package,record)
        if not missing:continue
        if record['pass_id']!=max(by_task[task['task_id']],key=lambda p:p['sequence'])['pass_id']:
            raise ContractError('STALE_INPUT','$','dependency gap is not in the latest citation')
        for gap in missing:
            gap['other_planned_coverage']=[{'task_id':t['task_id'],'target_refs':t['target_refs'],
                'current_inspection_covering_passes':[p['pass_id'] for p in selected.values() if p['task_id']==t['task_id']
                    and any(_covers(i['source_ref'],gap['missing_ref']) for i in p['inspections'])]}
                for t in tasks if any(r['unit_ref']==gap['missing_ref']['unit_ref'] for r in t['target_refs'])]
        gaps.extend(missing)
        items.append({'task_id':task['task_id'],'pass_id':record['pass_id'],'reason':'Append an authored citation after inspecting the currently required original dependencies'})
    report={'workflow_id':wid,'batch_id':w['batch_id'],'failure_bindings':failures,'gaps':gaps,
            'note':'Other-task inspection does not fill this citation. Original packages and Passes are immutable.'}
    ref=f'_system/reports/reduce-dependency-{rid}.json';path=_vault_path(a.workflow.vault,ref)
    data=_json_bytes(report)
    if path.exists() and path.read_bytes()!=data:raise ContractError('IDEMPOTENCY_CONFLICT','$','repair evidence differs')
    if not path.exists():_write_atomic(path,data)
    output.mkdir(parents=True,exist_ok=True)
    def request(name,**fields):
        value={'workflow_id':wid,'actor':w['actor'],'expected_revision':w['revision'],**fields}
        value['input_digest']=mutation_digest(value)
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if budget:
        request('budget-request.json',recovery_id=rid+'-packets',recovery_scope='source-packets/v1',
            reason='User authorized bounded complete-evidence source packets after the oversized whole-source input',
            entries=budget,target_contract_sha256=contract_hash())
    if items:
        request('revision-request.json',revision_id=rid+'-dependencies',revision_scope='reduce-dependencies/v1',
            evidence_digest='sha256:'+fingerprint(w['pause_control']),tasks=items,reduce_failures=failures,
            reason='User authorized selected historical citation supplement review before any Reduce consumption',
            evidence_refs=[ref],worker_template=(Path(__file__).resolve().parents[1]/'references/workers/pass-slice.md').read_text(encoding='utf-8'))
    return {'ok':True,'workflow_id':wid,'budget_nodes':len(budget),'revision_tasks':len(items),
            'missing_refs':len(gaps),'evidence_ref':ref,'resumed':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ('vault','workflow-id','repair-id','output-dir'):p.add_argument('--'+flag,required=True)
    args=p.parse_args()
    try:
        with record_validation_scope():result=prepare(args.vault,args.workflow_id,args.repair_id,Path(args.output_dir))
        print(json.dumps(result,ensure_ascii=False))
    except (ContractError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False),file=sys.stderr);raise SystemExit(2)
