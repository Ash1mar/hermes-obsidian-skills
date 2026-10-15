#!/usr/bin/env python3
"""Prepare an explicit migration request; never mutate workflow or dispatch."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'lib'))
from hermes_source_units import ContractError, FileIngestWorkflowService, mutation_digest
from hermes_source_units.validation import fingerprint
from reduce_execution_migration import KINDS, target_hashes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault',required=True)
    parser.add_argument('--workflow-id',required=True)
    parser.add_argument('--migration-id',required=True)
    parser.add_argument('--reason',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
            raise ContractError('ACCESS_DENIED','$','migration requires a trusted operator')
        service=FileIngestWorkflowService(args.vault)
        value=service.status(args.workflow_id)
        request={'workflow_id':value['workflow_id'],'actor':value['actor'],'expected_revision':value['revision'],
            'migration_id':args.migration_id,'migration_scope':'reduce-fixed/v1','reason':args.reason,
            'previous_pins_digest':'sha256:'+fingerprint([p for p in value['template_pins'] if p['kind'] in KINDS]),
            'target_template_hashes':target_hashes()}
        request['input_digest']=mutation_digest(request)
        output=Path(args.output)
        output.write_text(json.dumps(request,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'ok':True,'request_path':str(output),'workflow_id':value['workflow_id'],'resumed':False}))
        return 0
    except (ContractError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)}),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
