#!/usr/bin/env python3
"""Prepare/serve or revoke Linux-sealed evidence; never resume domain work."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from ingest_kanban import IngestKanbanAdapter
from ingest_graph_reuse import dependencies
from hermes_source_units.evidence_snapshot import Protector,ROOT,rpc,flags,IMMUTABLE,_dump,_lease,_start,assert_sealable_slices


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=('serve','revoke','release-stale','status'))
    p.add_argument('--vault',required=True);p.add_argument('--workflow-id',required=True)
    p.add_argument('--reason',default='controlled maintenance')
    p.add_argument('--protect-schema',action='store_true',help='Protect the exact installed reader schema under its separate host asset contract')
    args=p.parse_args();adapter=IngestKanbanAdapter(args.vault);service=adapter.workflow
    path=service.vault/ROOT/args.workflow_id
    if args.operation=='status':print((path/'state.json').read_text());return
    if args.operation=='revoke':
        state=json.loads((path/'state.json').read_text())
        if state['state']=='revoked':print(json.dumps({'state':'revoked'}));return
        print(json.dumps(rpc(state['socket'],{'operation':'revoke','reason':args.reason})));return
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise RuntimeError('native consumers cannot prepare or release protected evidence')
    if args.operation=='release-stale':
        state=json.loads((path/'state.json').read_text())
        if _start(state['pid'])==state['process_start']:raise RuntimeError('live protector must revoke its own generation')
        # Operator must quiesce consumers first. Do not trust a stale active flag.
        value=json.loads((path/'manifest.json').read_text()) if (path/'manifest.json').exists() else {'original_flags':{}}
        if value.get('generation') not in (None,state['generation']):raise RuntimeError('stale release generation mismatch')
        with _lease(path,True):
            flags(path,flags(path)&~IMMUTABLE);flags(path/'state.json',flags(path/'state.json')&~IMMUTABLE)
            state.update(state='revoked',reason=args.reason);_dump(path/'state.json',state)
            for name,bits in value['original_flags'].items():
                target=Path(name)
                from hermes_source_units.schema_assets import release_allowed
                if not release_allowed(service.vault,target,value):raise RuntimeError('release path outside supported evidence scopes')
                flags(target,bits)
            for name in ('manifest.json','lease.lock'):
                if (path/name).exists():flags(path/name,flags(path/name)&~IMMUTABLE)
        print(json.dumps({'state':'revoked','stale_protector':True}));return
    w=service.status(args.workflow_id)
    if w['state']!='cancelled':raise RuntimeError('prepare requires the supported stopped workflow')
    # No worker processes/leases may survive the caller's normal deployment and
    # recovery handoff. Completed slices cannot be frozen while a Pass is active.
    assert_sealable_slices(w,service.knowledge._slices(w['batch_id']))
    graph=dependencies(service,w,scan_role='snapshot_preparation')
    from hermes_source_units.schema_assets import installed_schema_asset
    protector=Protector(service,w,schema_asset=installed_schema_asset() if args.protect_schema else None)
    protector.prepare(graph)
    try:protector.serve()
    except KeyboardInterrupt:pass


if __name__=='__main__':main()
