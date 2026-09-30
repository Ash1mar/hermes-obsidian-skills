#!/usr/bin/env python3
"""Preview/apply a governed pre-batch repair; never resume or dispatch work."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'lib'))
from hermes_source_units import ContractError, FileIngestWorkflowService, mutation_digest
from hermes_source_units.source_units import _vault_path
from hermes_source_units.validation import fingerprint
from ingest_kanban import KanbanCLI
from orchestration import worker_pack


def prepare(vault, workflow_id, hashes, repair_id, evidence_refs):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise ContractError('ACCESS_DENIED','$','repair must run in an operator session')
    service = FileIngestWorkflowService(vault)
    value = service.status(workflow_id)
    if not value['cancel_requested'] or value['batch_id'] is not None:
        raise ContractError('INVALID_TRANSITION','$','repair requires cancelled workflow with no batch')
    selected = [item for item in value.get('source_outcomes', []) if item['content_sha256'] in hashes]
    if (len(selected) != len(set(hashes)) or any(item['status']!='failed' or
            item['error_code']!='SOURCE_UNIT_VALIDATION_FAILED' for item in selected)):
        raise ContractError('STALE_INPUT','$','select existing failed SourceUnit outcomes by exact raw SHA-256')
    refs = sorted({*evidence_refs, *(ref for item in selected for ref in item['artifact_refs'])})
    if not refs:
        raise ContractError('ARTIFACT_REQUIRED','$','repair requires explicit Vault evidence when no source is reset')
    if any(not _vault_path(service.vault, ref).is_file() for ref in refs):
        raise ContractError('ARTIFACT_REQUIRED','$','repair evidence must exist in the Vault')
    request = {'workflow_id':workflow_id,'actor':value['actor'],
        'expected_revision':value['revision'],'repair_id':repair_id,
        'reason':'Install validated current worker contracts; preserve outcomes except explicitly selected SourceUnit engine failures',
        'evidence_refs':refs,'templates':worker_pack(),
        'reset_sources':[{'path':item['path'],'outcome_digest':'sha256:'+fingerprint(item)} for item in selected]}
    request['input_digest'] = mutation_digest(request)
    return service, value, request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault',required=True)
    parser.add_argument('--workflow-id',required=True)
    parser.add_argument('--reset-source-sha256',action='append',default=[])
    parser.add_argument('--evidence-ref',action='append',default=[])
    parser.add_argument('--repair-id',required=True)
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    try:
        service,value,request = prepare(args.vault,args.workflow_id,args.reset_source_sha256,args.repair_id,args.evidence_ref)
        if not args.apply:
            print(json.dumps({'ok':True,'preview':True,'request':request},ensure_ascii=False,indent=2))
            return 0
        board = value['kanban']['board_id']
        cli = KanbanCLI()
        for item in value['kanban']['task_map']:
            if board and cli.task_status(board,item['task_id'])!='archived':
                cli.command('kanban','--board',board,'archive',item['task_id'])
        updated = service.repair_preparation(request)
        print(json.dumps({'ok':True,'workflow_id':updated['workflow_id'],
            'revision':updated['revision'],'state':updated['state'],
            'source_coverage':service.source_coverage(updated),
            'resumed':False,'repair_id':args.repair_id},ensure_ascii=False,indent=2))
        return 0
    except (ContractError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
