"""Isolated efficiency contracts; no real Vault, native worker or model request."""
import copy,json,sys
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hermes-obsidian-governed-ingest-orchestrator/lib'))
from efficiency_presentation import qa_present,qa_restore,page_evidence_view,classify_block,pair_preview,enabled
from efficiency_metrics import calls_report
from fixed_semantic_schema import slot_schema,slot_results
from hermes_source_units import ContractError
from hermes_source_units.release_reads import ReleaseReads

def test_qa_roundtrip_preserves_nonidentical_notes():
    qa=[{'qa':'needs-qa','qa_note':'condition '*100}]
    v={'candidates':[{'candidate':'c1','evidence_qa':qa,'source_evidence':[]},
                     {'candidate':'c2','evidence_qa':copy.deepcopy(qa),'source_evidence':[]},
                     {'candidate':'c3','evidence_qa':[{'qa':'needs-qa','qa_note':'different'}]}]}
    before=copy.deepcopy(v);packed=qa_present(v)
    assert qa_restore(packed)==before==v
    assert packed['candidates'][0]['evidence_qa']==packed['candidates'][1]['evidence_qa']
    assert packed['candidates'][2]['evidence_qa']!=packed['candidates'][0]['evidence_qa']
    assert len(json.dumps(packed))<len(json.dumps(v))

def test_context_fallback_retains_every_field():
    p={'tasks':[{'task':'t1','materials':[{'ref':'m1','heading':0},{'ref':'m2','required_dependency':True}], 'missing':['table']}], 'headings':[['scope']], 'qa':{'q1':'limit'}}
    c=[{'source_evidence':[{'reading':'t1','support_refs':['m1']}]}]
    view,manifest=page_evidence_view(p,c)
    assert view==p and manifest['selection'][0]['retained_refs']==['m1','m2']
    c[0]['source_evidence'][0]['support_refs']=['m3']
    with pytest.raises(ContractError):page_evidence_view(p,c)

def test_blocked_unchanged_evidence_has_no_mandatory_retry():
    assert classify_block({'blocked_reason':'identity mismatch'})['automatic_correction'] is False
    assert classify_block({'content':'valid'})['automatic_correction'] is True

def test_unique_candidate_inspection_slots():
    tasks=[{'task':'t1','inspection_slots':'unique-material/v1','materials':[{'ref':'m1'},{'ref':'m2'}]}]
    sc=slot_schema('candidate',tasks)
    branch=sc['properties']['results']['properties']['t1']['anyOf'][0]
    slots=branch['properties']['inspections']
    assert set(slots['properties'])=={'m1','m2'} and slots['additionalProperties'] is False
    assert 'source_ref' not in slots['properties']['m1']['properties']
    bad=slot_results('candidate',{'results':{'t1':{'inspections':{'m1':{}}}}},tasks)
    assert bad['slot_failures'][0]['code']=='INCOMPLETE_COVERAGE'

def test_sdk_identity_not_transport_or_audits():
    events=[{'name':'sdk_request_start','span_id':'s1','request_id':'r1','phase':'candidate'},
            {'name':'sdk_model_request','span_id':'e1','request_id':'r1','phase':'candidate'},
            {'name':'sdk_request_start','span_id':'s2','request_id':'r2','phase':'citation'}]
    events += [{'name':'sdk_transport_entry','span_id':str(i),'request_id':'r1'} for i in range(3)]
    r=calls_report([],events,[])
    assert r['actual_sdk_requests']==2 and r['unknown_requests']==1 and len(r['transport_entries'])==3

def test_only_immutable_review_ledger_is_cacheable(tmp_path):
    reads=ReleaseReads(tmp_path)
    assert reads.accepts(tmp_path/('_system/ledgers/ingest-workflows/w/semantic-inputs/review-'+'a'*64+'.json'))
    assert not reads.accepts(tmp_path/'_system/ledgers/ingest-workflows/w/semantic-inputs/review.json')
    assert not reads.accepts(tmp_path/'_system/ledgers/ingest-workflows/w.json')
    assert not reads.accepts(tmp_path/'_system/ledgers/ingest-workflows/w/automatic-control/x.json')
    path=tmp_path/'data.json';path.write_text('{"x":1}')
    reads.load(path);reads.load(path);assert reads.stats['reads']==1
    path.write_text('{"x":2}')
    with pytest.raises(ContractError):reads.confirm()

def test_presentation_profile_freezes_legacy_and_new(tmp_path):
    from hermes_source_units.source_units import _write_atomic,_json_bytes
    w={'workflow_id':'w','scope':{}};binding={'workflow_id':'w'}
    adapter=NS(workflow=NS(vault=tmp_path,status=lambda _:w),worker_check=lambda _:None)
    assert enabled(adapter,binding)
    root=tmp_path/'_system/ledgers/ingest-workflows/w';(root/'semantic-calls').mkdir()
    (root/'semantic-calls/x.json').write_text('{}')
    assert enabled(adapter,binding)
    (root/'presentation-contract.json').unlink()
    assert not enabled(adapter,binding)

def test_two_page_preview_is_bounded_and_does_not_dispatch():
    pages=[{'source_scope':'same','evidence_complete':True,'path':str(i)} for i in range(2)]
    assert pair_preview(pages,lambda _:10)['dispatch_authorized'] is False
    pages[0]['risk_reasons']=['uncertain']
    with pytest.raises(ContractError):pair_preview(pages,lambda _:10)

def test_nested_board_projection_reuses_one_list():
    from ingest_kanban import KanbanCLI
    import subprocess
    calls=[]
    def invoke(argv):
        calls.append(argv);return subprocess.CompletedProcess(argv,0,'[]','')
    kb=KanbanCLI(invoke)
    with kb.projection('b'):
        with kb.projection('b'):assert kb.projected_task_states('b')=={}
    assert len(calls)==1
