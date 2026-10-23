#!/usr/bin/env python3
"""Prepare and dry-run an explicit stopped Reduce disposition; never resume."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from hermes_source_units import mutation_digest
from hermes_source_units.validation import fingerprint
from hermes_source_units.source_units import _load_json,_vault_path,_json_bytes,_write_atomic
from ingest_kanban import IngestKanbanAdapter
from native_stop_inventory import verify_cancel_drain
from reduce_disposition import SCOPE,contract_hash
from orchestration import dispatch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault',required=True)
    parser.add_argument('--workflow-id',required=True)
    parser.add_argument('--recovery-id',required=True)
    parser.add_argument('--selections',required=True,help='JSON list: attempt_ref, action, reason; no domain answers')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    adapter=IngestKanbanAdapter(args.vault);w=adapter.workflow.status(args.workflow_id)
    verify_cancel_drain(adapter,args.workflow_id,timeout=0)
    card=next(c for c in w['kanban']['task_map'] if c['node']=='global-reduce')
    key=card['idempotency_key']
    root=f'_system/ledgers/ingest-workflows/{args.workflow_id}'
    binding=_load_json(_vault_path(args.vault,f'{root}/bindings/{fingerprint(key)}.json'))
    import fixed_reduce
    value=fixed_reduce.preview_snapshot(adapter,binding)
    # A current immutable native snapshot must already exist. Preparation never
    # fabricates a domain result or changes a previous reading package.
    candidates=[]
    for path in (_vault_path(args.vault,root)/'semantic-inputs').glob('*.json'):
        if path.name.startswith(('global-progress-','page-work-','packets-')):continue
        row=_load_json(path)
        if row==value:candidates.append(path)
    if len(candidates)!=1:raise RuntimeError('one exact current native snapshot is required')
    source=candidates[0]
    failure=_vault_path(args.vault,f'{root}/reports/failed-{fingerprint(card["node"])[:16]}.json')
    outputs=json.loads(Path(args.selections).read_text(encoding='utf-8-sig'))
    for row in outputs:
        path=_vault_path(args.vault,row['attempt_ref'])
        row['attempt_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    entry={'node':card['node'],'task_id':card['task_id'],
        'failure_sha256':hashlib.sha256(failure.read_bytes()).hexdigest(),
        'snapshot_ref':source.relative_to(Path(args.vault)).as_posix(),
        'snapshot_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'outputs':outputs}
    request={'workflow_id':args.workflow_id,'actor':w['actor'],'expected_revision':w['revision'],
        'recovery_id':args.recovery_id,'recovery_scope':SCOPE,'target_contract_sha256':contract_hash(),
        'reason':'Explicit user-authorized bounded Reduce dispositions; preserve valid work and old calls; '
            'no acceptance of necessary coverage gaps, no automatic correction, stop at checkpoint_1.',
        'entries':[entry]}
    request['input_digest']=mutation_digest(request)
    result=dispatch(args.vault,'recover-reduce-execution',request,dry_run=True)
    output=Path(args.output)
    _write_atomic(output,_json_bytes(request))
    _write_atomic(output.with_suffix('.preview.json'),_json_bytes(result))
    print(json.dumps({'ok':result['ok'],'selected_outputs':len(outputs),'applied':False,'resumed':False,
        'request_path':str(output)},ensure_ascii=False))


if __name__=='__main__':main()
