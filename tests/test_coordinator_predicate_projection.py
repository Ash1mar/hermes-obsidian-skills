"""Fresh predicate boundaries, lazy revision materials and first-match cards."""
from types import SimpleNamespace as NS
from pathlib import Path
import pytest
from hermes_source_units.verified_json import VerifiedJsonReads
from hermes_source_units.validation import fingerprint
import ingest_kanban as kanban


def snapshot(tmp_path,value):
    import json
    p=tmp_path/'snapshot.json';p.write_text(json.dumps(value))
    cache=VerifiedJsonReads()
    return cache.read(p,readonly=True,origin='coordinator-predicate'),cache


def test_first_match_is_detached_and_never_copies_large_history(tmp_path):
    value={'kanban':{'task_map':[{'node':'n','task_id':'first'},{'node':'n','task_id':'last'}]},
        'pass_revision_history':[{'protected_files':['x'*1000]*1000}]}
    snap,cache=snapshot(tmp_path,value)
    chosen=kanban._first_node_card(snap,'n');assert chosen['task_id']=='first';chosen['task_id']='changed'
    assert kanban._first_node_card(snap,'n')['task_id']=='first'
    assert cache.stats['full_copies']==0 and cache.stats['projected_copy_nodes']<30


def test_revision_material_is_lazy_small_and_preserves_first_success(tmp_path,monkeypatch):
    r={'revision_scope':'reduce-dependencies/v1','state':'accepted','slice_ids':['s'],
        'revision_id':'r','tasks':[{'task_id':'t','pass_id':'p','sequence':1}],
        'protected_files':['x'*1000]*1000}
    snap,cache=snapshot(tmp_path,{'pass_revision_history':[r,{**r,'revision_id':'later'}]})
    seen=[]
    import hermes_source_units.reduce_task_skip as skip
    def covered(service,workflow,revision,sid):seen.append(revision);return True
    monkeypatch.setattr(skip,'revision_slice_covered',covered)
    assert kanban._revision_coverage(None,snap,'s')
    assert seen==[{'revision_id':'r','tasks':[{'task_id':'t','pass_id':'p','sequence':1}]}]
    assert cache.stats['full_copies']==0 and cache.stats['projected_copy_nodes']<50


def test_failed_card_reads_again_at_original_boundary(tmp_path,monkeypatch):
    value={'kanban':{'task_map':[{'node':'n','task_id':'first'}]}}
    snap,cache=snapshot(tmp_path,value);reads=[]
    service=NS(vault=tmp_path,status_snapshot=lambda wid,**kw:(reads.append(kw['origin']) or snap))
    adapter=NS(workflow=service)
    monkeypatch.setattr(kanban,'failure_record',lambda path:{'input_fingerprint':'f','task_id':'old'})
    node=kanban.Node('n','pass-slice',(),'f')
    monkeypatch.setattr(kanban,'domain_completed',lambda *args:True)
    assert not kanban.IngestKanbanAdapter._failed_card(adapter,'w',node)
    assert reads==['reconciliation.failed_card']
    monkeypatch.setattr(kanban,'failure_record',lambda path:{'input_fingerprint':'different'})
    assert not kanban.IngestKanbanAdapter._failed_card(adapter,'w',node)
    assert len(reads)==1
