"""A coverage gap cannot invalidate its independently completed sibling task."""
import copy
from test_compact_execution import stopped,vault
from ingest_kanban import desired_graph,domain_completed
from hermes_source_units.source_units import _write_atomic,_json_bytes


def test_reduce_uses_complete_sibling_without_calling_gap_success(stopped,monkeypatch):
    adapter,value,_=stopped;k=adapter.workflow.knowledge
    item=next(s for s in k._slices(value['batch_id']) if s['state']=='completed')
    assert len(item['task_ids'])==2
    valid,excluded=item['task_ids'];gap='pass-slice:'+item['slice_id']
    task=k._task(excluded);task.update(status='skipped',reason='isolated fixture gap',revision=task['revision']+1)
    _write_atomic(k._task_path(excluded),_json_bytes(task))
    item['state']='cancelled';_write_atomic(k._slice_path(value['batch_id'],item['slice_id']),_json_bytes(item))
    value=copy.deepcopy(value);value['current_stage']='reducing';value['scope']['pause_after']=[]
    monkeypatch.setattr(adapter.workflow,'skipped_pass_slices',lambda _: {item['slice_id']})
    checked=[];monkeypatch.setattr(adapter.workflow,'verify_pass_skips',lambda _:checked.append(True))
    nodes=desired_graph(adapter.workflow,value)
    reducer=next(n for n in nodes if n.kind=='resource-reduce')
    assert checked and gap not in reducer.parents
    assert not domain_completed(adapter.workflow,value,next(n for n in nodes if n.name==gap))
    original=adapter.workflow._task_passes_complete
    monkeypatch.setattr(adapter.workflow,'_task_passes_complete',lambda tid:False if tid==valid else original(tid))
    nodes=desired_graph(adapter.workflow,value)
    assert gap in next(n for n in nodes if n.kind=='resource-reduce').parents
