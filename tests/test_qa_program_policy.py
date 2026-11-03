"""All-stage QA is advisory; known blocked work is isolated without adjudication."""
import json,sys
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hermes-obsidian-governed-ingest-orchestrator/lib'))
from hermes_source_units.qa_policy import POLICY,STAGES,disposition,gap,reports
from hermes_source_units import ContractError
from qa_program import isolate_pages
from test_refill_handoff import refill


def test_qa_labels_never_require_a_human_or_a_model_in_any_stage():
    for _ in STAGES:
        assert disposition(blocking=False)=='continue_with_qa_metadata'
        assert disposition(blocking=True,additional_checks=0)=='isolate_with_dependents'
        assert disposition(blocking=True,additional_checks=1)=='isolate_with_dependents'
        assert disposition(blocking=True,known_response=False)=='hold_execution_uncertainty'
    assert POLICY['human_review_required'] is False and POLICY['max_additional_hermes_checks']==1


def test_qa_isolates_only_actual_page_dependents_preserving_native_bodies(tmp_path):
    adapter=NS(workflow=NS(vault=tmp_path,status=lambda _:{'scope':{'execution_mode':'auto_full'}}))
    binding={'workflow_id':'ingest-qa','node':'global-reduce'}
    pages=[{'path':f'30_Cards/{n}.md','identity':{'canonical_name':n,'aliases':[]},'candidate_refs':[n]} for n in ('a','b','c')]
    state={'pages':pages,'omitted_candidate_refs':[],'reasons':[]}
    written=[{**pages[0],'content':'# A\nSee [[b]].'},{**pages[2],'content':'# C\nIndependent verified material.'}]
    held=[{'path':pages[1]['path'],'code':'SEMANTIC_REVIEW_REQUIRED','reason':'Required evidence cannot verify this page','audit_ref':'native.json'}]
    result,ref=isolate_pages(adapter,binding,{},state,written,held,None)
    assert [p['path'] for p in result['pages']]==['30_Cards/c.md']
    assert set(result['omitted_candidate_refs'])=={'a','b'}
    assert len(written)==2 and len(state['pages'])==3  # No native result is overwritten.
    assert reports(tmp_path,'ingest-qa')==[ref]
    record=json.loads((tmp_path/ref).read_text());assert record['human_review_requested'] is False
    record['coverage_complete']=True;(tmp_path/ref).write_text(json.dumps(record))
    with pytest.raises(ContractError,match='QA gap evidence changed'):reports(tmp_path,'ingest-qa')


@pytest.mark.parametrize('host_cap,expected',[ (None,12),(10,10) ])
def test_reduce_ten_is_separate_from_other_program_two_and_respects_host_cap(refill,monkeypatch,host_cap,expected):
    import native_program_dispatch as d
    from hermes_cli import kanban_db_dispatch as kd
    original=refill.adapter.workflow.status
    refill.adapter.workflow.status=lambda wid:{**original(wid),'scope':{'execution_policy':{'reduce_worker_concurrency':10}}}
    refill.adapter.reduce_worker_concurrency=10
    monkeypatch.setattr(kd,'resolve_max_in_progress',lambda _:host_cap)
    refill.cards.clear();base=NS(**vars(refill.tasks[0]));refill.tasks.clear()
    for i in range(14):
        tid=f't{i}';kind='resource-reduce:' if i<11 else 'program:'
        refill.cards.append({'task_id':tid,'node':kind+tid,'idempotency_key':'key-'+tid})
        task=NS(**vars(base));task.id=tid;task.idempotency_key='key-'+tid;task.status='ready'
        task.body=json.dumps({'executor':'semantic-v1','workflow_id':'ingest-refill','worker_binding':tid})
        refill.tasks.append(task)
    spawned=d.dispatch_programs(refill.adapter,'ingest-refill')
    assert len(spawned)==expected and refill.tasks[10].status=='ready'
    assert sum(t.status=='running' for t in refill.tasks[11:])==(2 if host_cap is None else 0)
