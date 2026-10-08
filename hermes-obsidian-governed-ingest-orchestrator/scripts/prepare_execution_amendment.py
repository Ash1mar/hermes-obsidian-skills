#!/usr/bin/env python3
"""Prepare an operator request; never amend, resume or perform domain work."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from hermes_source_units import ContractError, FileIngestWorkflowService, mutation_digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--workflow-id', required=True)
    parser.add_argument('--amendment-id', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--max-tasks', type=int, default=12)
    parser.add_argument('--max-input-codepoints', type=int, default=30000)
    parser.add_argument('--output', required=True, help='Request file in the operator workspace, outside the Vault')
    args = parser.parse_args()
    try:
        if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
            raise ValueError('only a trusted operator can prepare an execution amendment')
        service = FileIngestWorkflowService(args.vault)
        value = service.status(args.workflow_id)
        output = Path(args.output).resolve()
        if output.is_relative_to(service.vault) or output.exists():
            raise ValueError('output must be a new request file outside the Vault; reuse an existing request on retry')
        template = Path(__file__).resolve().parents[1] / 'references/workers/pass-slice-compact.md'
        request = {'workflow_id':value['workflow_id'], 'actor':value['actor'],
            'expected_revision':value['revision'], 'amendment_id':args.amendment_id,
            'reason':args.reason, 'max_tasks':args.max_tasks,
            'max_input_codepoints':args.max_input_codepoints,
            'worker_template':template.read_text(encoding='utf-8')}
        request['input_digest'] = mutation_digest(request)
        with output.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(request, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        print(json.dumps({'ok':True, 'request_path':str(output), 'input_digest':request['input_digest'],
                          'workflow_id':value['workflow_id'], 'applied':False}, ensure_ascii=False))
        return 0
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({'ok':False, 'error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
