"""Instrument actual old reads, retaining confirmation and cache invalidation."""
import json
from types import SimpleNamespace as NS
from hermes_source_units.read_accounting import accounting_scope
from ingest_completion import completion_scope,completed,failure_record
from ingest_kanban import Node


def test_completion_and_failure_confirmation_reads_are_counted(tmp_path):
    p=tmp_path/'slice.json';p.write_text('{"state":"completed"}')
    service=NS(vault=tmp_path,knowledge=NS(_slice_path=lambda *_:p))
    node=Node('pass-slice:s','pass-slice',(),'f');w={'batch_id':'b','workflow_id':'w'}
    with accounting_scope() as ledger,completion_scope():
        assert completed(service,w,node,lambda:True)
        assert completed(service,w,node,lambda:False)  # Existing valid pure proof.
        assert sum(r['reads'] for r in ledger.rows.values())==3
        assert {r['origin'] for r in ledger.rows.values()}=={'completion.slice_signature','completion.slice_confirmation'}
        f=tmp_path/'failed.json';f.write_text('{"error_code":"held"}')
        assert failure_record(f)['error_code']=='held'
        assert any(r['origin']=='completion.failure_confirmation' for r in ledger.rows.values())
