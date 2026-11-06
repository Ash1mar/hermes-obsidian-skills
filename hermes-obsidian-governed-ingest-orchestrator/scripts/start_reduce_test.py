#!/usr/bin/env python3
"""Test-only same-workflow Reduce reentry with verified Pass reuse and audit."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'lib'))
from watcher_runtime import ensure_native_runtime
from reduce_test import CONTRACT, reenter, describe
from ingest_kanban import IngestKanbanAdapter
from orchestration import dispatch, _mutation
from hermes_source_units.source_units import _write_atomic, _json_bytes
from hermes_source_units.validation import fingerprint


def main():
    ensure_native_runtime()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--request', required=True, type=Path)
    p.add_argument('--vault', required=True, type=Path)
    p.add_argument('--record', type=Path)
    p.add_argument('--plan', action='store_true')
    p.add_argument('--workflow-id')
    p.add_argument('--operation-id')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    try:
        adapter = IngestKanbanAdapter(a.vault)
        if a.plan:
            if not a.workflow_id or not a.operation_id:raise ValueError('plan requires workflow and stable operation IDs')
            w = adapter.workflow.status(a.workflow_id)
            prefix = describe(a.vault,a.workflow_id)
            request = _mutation(w,test_mode=CONTRACT,operation_id=a.operation_id,
                overwrite_downstream=True,prefix_sha256=fingerprint(prefix))
            if a.request.exists() and json.loads(a.request.read_text()) != request:
                raise ValueError('planned request exists with different inputs')
            if not a.request.exists():_write_atomic(a.request,_json_bytes(request))
            print(json.dumps({'ok':True,'request':str(a.request),'workflow_id':a.workflow_id,
                'reused_tasks':len(prefix['task_ids']),'model_calls':0,'resumed':False}))
            return 0
        request = json.loads(a.request.read_text(encoding='utf-8-sig'))
        if request.get('test_mode') != CONTRACT:
            raise ValueError('explicit Reduce test request required')
        if not a.record:raise ValueError('execution requires an external receipt path')
        result = reenter(adapter, request, dry_run=a.dry_run)
        if not a.dry_run:
            # The durable journal admits once. A retry of this entry only observes
            # a running/finished cycle; it cannot rewind or grant calls again.
            w = adapter.workflow.status(request['workflow_id'])
            if (w['cancel_requested'] and w['state'] == 'cancelled'
                    and w['revision'] == request['expected_revision'] + 1):
                from hermes_source_units.presentation_contract import pin
                from page_protocol import preflight
                preflight(pin(adapter.workflow, w))
                result['handoff'] = dispatch(a.vault, 'resume', _mutation(w))
                from watcher_runtime import confirm_handoff
                result['watcher'] = confirm_handoff(a.vault,w['workflow_id'],result['handoff'].get('reconciler_pid'))
            elif not w['cancel_requested'] and w['state'] not in ('completed','partial','failed') and not adapter.workflow.pending_pause(w):
                from watcher_runtime import coordinator_pid,confirm_handoff
                pid=coordinator_pid(a.vault,w['workflow_id'])
                if not pid:
                    result['handoff']=dispatch(a.vault,'resume',_mutation(w))
                    pid=result['handoff'].get('reconciler_pid')
                result['watcher']=confirm_handoff(a.vault,w['workflow_id'],pid)
            else:
                result['workflow'] = adapter.workflow.status(w['workflow_id'],compact=True)
            _write_atomic(a.record, _json_bytes(result))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
