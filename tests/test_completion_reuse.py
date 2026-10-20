"""Cycle-local proof invalidation, no native grant or semantic acceptance."""
import json
from types import SimpleNamespace as NS
from ingest_completion import completion_scope,completed,resource_record,failure_record
from ingest_kanban import Node,KanbanCLI


def test_completed_slice_rechecks_content_even_with_same_revision_and_timestamp(tmp_path):
    path=tmp_path/'slice.json';path.write_text('{"state":"completed"}')
    k=NS(_slice_path=lambda *_:path);s=NS(knowledge=k,vault=tmp_path);w={'workflow_id':'w','batch_id':'b','revision':1}
    n=Node('pass-slice:s','pass-slice',(),'hash');calls=[]
    def build():calls.append(1);return json.loads(path.read_text())['state']=='completed'
    with completion_scope():
        assert completed(s,w,n,build) and completed(s,w,n,build) and len(calls)==1
        path.write_text('{"state":"cancelled"}');assert not completed(s,w,n,build) and len(calls)==2
        path.write_text('{"state":"completed"}');assert completed(s,w,n,build)
    with completion_scope():assert completed(s,w,n,build) and len(calls)==3


def test_resource_membership_and_record_bytes_invalidate_pure_proofs(tmp_path):
    bp=tmp_path/'batch.json';bp.write_text('{"resource_reduction_ids":["one"]}');(tmp_path/'one').write_text('{"resource_id":"A"}')
    reads=[]
    def read(_b,rid):reads.append(rid);return json.loads((tmp_path/rid).read_text())
    k=NS(_batch=lambda _:json.loads(bp.read_text()),_batch_path=lambda _:bp,_resource_reduction_path=lambda _b,r:tmp_path/r,_resource_reduction=read)
    s=NS(knowledge=k,vault=tmp_path);w={'workflow_id':'w','batch_id':'b'};n=Node('resource-reduce:A','resource-reduce',(),'hash')
    def build():return any(resource_record(k,'b',rid)['resource_id']=='A' for rid in k._batch('b')['resource_reduction_ids'])
    with completion_scope():
        assert completed(s,w,n,build) and completed(s,w,n,build) and reads==['one']
        row=resource_record(k,'b','one');row['resource_id']='mutated returned copy';assert completed(s,w,n,build)
        (tmp_path/'one').write_text('{"resource_id":"B"}');assert not completed(s,w,n,build)
        (tmp_path/'two').write_text('{"resource_id":"A"}');bp.write_text('{"resource_reduction_ids":["one","two"]}')
        assert completed(s,w,n,build)


def test_failure_index_sees_new_changed_and_deleted_files(tmp_path):
    p=tmp_path/'failed.json'
    with completion_scope():
        assert failure_record(p) is None
        p.write_text('{"reason":"one"}');assert failure_record(p)['reason']=='one'
        p.write_text('{"reason":"two"}');assert failure_record(p)['reason']=='two'
        p.unlink();assert failure_record(p) is None


def test_projection_retains_only_unchanged_terminal_rows_and_never_fresh_grants(monkeypatch):
    cli=KanbanCLI();cli._projection_board='b';cli._projection_rows={'closed':{'status':'done'},'live':{'status':'running'}}
    cli._projection_snapshots={'closed':{'task':{'status':'done'}},'live':{'task':{'status':'running'}}}
    lists=[]
    monkeypatch.setattr(cli,'task_states',lambda _:lists.append(1) or {'closed':'done','live':'running'})
    assert cli.projected_task_states('b')=={'closed':'done','live':'running'} and not lists
    cli._refresh_projection('b');assert set(cli._projection_snapshots)=={'closed'}
    def changed(_):cli._projection_rows={**cli._projection_rows,'closed':{'status':'ready'}};return {}
    monkeypatch.setattr(cli,'task_states',changed);cli._refresh_projection('b');assert not cli._projection_snapshots
