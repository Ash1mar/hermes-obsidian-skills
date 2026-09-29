#!/usr/bin/env python3
"""Execute the trusted dispatcher's preparation binding without model-built JSON."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes, _load_json, _vault_path, _write_atomic
from hermes_source_units.validation import fingerprint
from ingest_kanban import IngestKanbanAdapter
from orchestration import dispatch


def run(vault, binding):
    adapter = IngestKanbanAdapter(vault, enable_workers=True)
    path = Path(binding).resolve()
    request = _load_json(path)
    adapter.validate_worker_request(request)
    value = adapter.workflow.status(request['workflow_id'])
    cards = [c for c in value['kanban']['task_map'] if c.get('task_id') == request['task_id']
             and c['node'] == request['node']]
    if len(cards) != 1 or path != _vault_path(adapter.workflow.vault,
            f"_system/ledgers/ingest-workflows/{value['workflow_id']}/bindings/{fingerprint(cards[0]['idempotency_key'])}.json").resolve():
        raise ContractError('STALE_INPUT', '$', 'binding file is not the current dispatcher artifact')
    command = {'exact-plan':'worker-plan-exact', 'source-prepare':'worker-prepare-source'}.get(
        request['node'].partition(':')[0])
    if command is None:
        raise ContractError('ACCESS_DENIED', '$', 'binding is not a preparation worker')
    progress = path.with_suffix('.execution.json')
    _write_atomic(progress, _json_bytes({'state':'starting', 'request_digest':fingerprint(request),
        'task_id':request['task_id'], 'command':command, 'started_at':datetime.now(timezone.utc).isoformat()}))
    try:
        adapter.worker_check(request)
        result = dispatch(vault, command, request)
    except (ContractError, OSError, ValueError, KeyError, RuntimeError) as exc:
        code = exc.code if isinstance(exc, ContractError) else 'WORKER_EXECUTION_FAILED'
        # A current card identity suffices to report a rejected request. It never
        # grants permission for a domain write or translates it to source damage.
        report = adapter.execution_failure(request, code, str(exc))
        _write_atomic(progress, _json_bytes({'state':'execution_blocked', 'code':code,
            'request_digest':fingerprint(request), 'report_ref':report}))
        raise
    _write_atomic(progress, _json_bytes({'state':'completed', 'request_digest':fingerprint(request),
        'finished_at':datetime.now(timezone.utc).isoformat(), 'result':result}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--binding', required=True)
    args = parser.parse_args()
    try:
        result = run(args.vault, args.binding)
    except (ContractError, OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({'ok':False, 'code':getattr(exc,'code','WORKER_EXECUTION_FAILED'),
                          'error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
