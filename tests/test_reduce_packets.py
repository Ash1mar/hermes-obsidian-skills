"""Bounded source proposal mechanics; no real-Vault semantic fixtures."""
import copy
from types import SimpleNamespace
import pytest
import fixed_semantic as fixed
import fixed_reduce_packets as packets
from hermes_source_units import ContractError
from test_p3_knowledge_build import vault,workflow_request


def value(size=8000):
    tasks=[{'task':f't{i}','materials':[{'ref':'m1','role':'core','text_ref':f'x{i}','heading':0}]} for i in (1,2,3)]
    return {'view':{'phase':'resource-reduce','candidates':[
        {'candidate':f'c{i}','finding':'original conditions retained','source_evidence':[{'reading':f't{i}','support_refs':['m1']}]} for i in (1,2,3)],
        'original_evidence':{'tasks':tasks,'texts':{f'x{i}':'e'*size for i in (1,2,3)},'headings':['Rule']}}}


def adapter():return SimpleNamespace(workflow=SimpleNamespace(knowledge=SimpleNamespace(source=None)))


def test_source_packets_fit_actual_request_and_preserve_whole_original_readings():
    original=value();before=copy.deepcopy(original)
    planned=packets.plan(adapter(),original,fixed.prepare_request_projection,limit=20000)
    assert len(planned)>1 and original==before
    assert [r for p in planned for r in p['candidate_refs']]==['c1','c2','c3']
    for p in planned:
        assert p['input_codepoints']+1024<=20000
        readings={r['reading'] for c in p['view']['candidates'] for r in c['source_evidence']}
        assert {t['task'] for t in p['view']['original_evidence']['tasks']}==readings
        assert all(len(text)==8000 for text in p['view']['original_evidence']['texts'].values())
    drafts=[{'proposals':[{'candidate_refs':p['candidate_refs'],'summary':'Source proposal',
        'identity_hints':[],'path_hints':[]}],'omitted_candidate_refs':[],'reason':'Isolated fixture'} for p in planned]
    merged=packets.merge(original,planned,drafts)
    assert [r for p in merged['proposals'] for r in p['candidate_refs']]==['c1','c2','c3']
    drafts[0]={'proposals':[],'omitted_candidate_refs':planned[0]['candidate_refs'],
               'reason':'Recorded truncated source; unverified claims excluded, not covered.'}
    loss=packets.merge(original,planned,drafts)
    assert loss['omitted_candidate_refs']==planned[0]['candidate_refs']
    assert drafts[0]['reason'] in loss['reason']


def test_one_indivisible_candidate_remains_blocked_before_any_call():
    with pytest.raises(ContractError,match='one candidate with its complete required evidence'):
        packets.plan(adapter(),value(40000),fixed.prepare_request_projection)


def test_packet_cannot_adopt_another_packets_candidates_or_omit_without_accounting():
    planned=packets.plan(adapter(),value(),fixed.prepare_request_projection,limit=14000)
    draft={'proposals':[],'omitted_candidate_refs':['c2'],'reason':'Invalid cross-packet omission'}
    with pytest.raises(ContractError,match='INCOMPLETE_COVERAGE'):packets.validate_packet(planned[0],draft)


def test_image_budget_is_checked_even_when_text_fits(monkeypatch):
    monkeypatch.setattr(packets,'images_fit',lambda _:False)
    with pytest.raises(ContractError,match='READING_WINDOW_OVERSIZE'):
        packets.plan(adapter(),value(20),fixed.prepare_request_projection)


def test_only_measured_corrected_source_budget_failure_can_get_a_fresh_identity(vault,monkeypatch):
    import hashlib,json
    from test_reduce_execution_recovery import stopped_reducer
    from hermes_source_units import mutation_digest
    a,w,card,request,ref=stopped_reducer(vault,monkeypatch)
    failure=json.loads((vault/ref).read_text())
    failure['error_code']='READING_WINDOW_OVERSIZE'
    failure['reason']='fixed task instructions/schema/materials exceed the bound'
    (vault/ref).write_text(json.dumps(failure))
    request.update(recovery_scope='source-packets/v1',target_contract_sha256=packets.contract_hash())
    request['entries'][0]['failure_sha256']=hashlib.sha256((vault/ref).read_bytes()).hexdigest()
    request['input_digest']=mutation_digest(request)
    before={p:p.read_bytes() for p in (vault/'_system/knowledge-builds').rglob('*') if p.is_file()}
    preview=a.recover_reduce_execution(request,dry_run=True)
    assert preview['entries'][0]['corrected_input']['packets']
    assert a.recover_reduce_execution(request)['applied']
    current=a.workflow.status(w['workflow_id'])
    a.resume(workflow_request(workflow_id=w['workflow_id'],actor='agent',expected_revision=current['revision']))
    current=a.workflow.status(w['workflow_id'])
    fresh=next(c for c in current['kanban']['task_map'] if c['node']==card['node'])
    assert fresh['task_id']!=card['task_id']
    assert all(p.read_bytes()==b for p,b in before.items() if 'passes' in p.parts or 'readings' in p.parts)
