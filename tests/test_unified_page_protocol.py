"""Future-only rules with realistic retained QA/limits, no native requests."""
import copy
import pytest
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint
from page_protocol import CONTRACT,BUDGET,preflight
from page_pair_eligibility import pair,inspect
from fixed_semantic import system_prompt
from test_page_call_contracts import packet
from page_dependencies import derive
from test_p3_knowledge_build import vault

def views():
    evidence,candidates,proof=packet()
    candidates[0]['evidence_qa'][0]['qa_note']='Scope limitation retained; assigned support is usable.'
    evidence['tasks'][0]['limits']['context_truncated']=True
    proof['packet_sha256']=fingerprint(evidence)
    rows=[]
    for i in range(2):
        c=copy.deepcopy(candidates);c[0]['candidate']='c'+str(i);c[0]['applicability']='separate scope '+str(i)
        e,p=derive(evidence,c,proof)
        rows.append(({'path':str(i)+'.md'},{'identity':{'kind':'entity','identity_key':str(i)},'path':str(i)+'.md',
            'candidates':c,'original_evidence':e,'evidence_selection':p,'page_execution_contract':CONTRACT}))
    return rows

def test_notes_truncation_and_different_scope_do_not_erase_pair_opportunity():
    rows=views();before=copy.deepcopy(rows);decision=pair(*rows)
    assert decision['eligible'] and all(r['retained_qa_notes']==1 for r in decision['pages'])
    assert rows==before and rows[0][1]['original_evidence']['tasks'][0]['limits']['context_truncated']

def test_missing_required_support_blocks_qualification_without_removing_notices():
    rows=views();v=rows[0][1];v['original_evidence']['tasks'][0]['materials'][0]['missing']=True
    assert inspect(v)['required_missing'] and not pair(*rows)['eligible']

def test_single_pair_and_review_scope_rules_are_shared():
    from reduce_disposition import PAGE_RULE
    v=views()[0][1]
    for phase,value in [('page-write',v),('page-pair',{'pages':{'p1':v},'page_execution_contract':CONTRACT})]:
        assert PAGE_RULE in system_prompt(phase,value)
    old={'pages':{'p1':{}}}
    assert PAGE_RULE not in system_prompt('page-pair',old) # historical wire stays exact

def test_incomplete_protocol_install_blocks_before_consumption(monkeypatch):
    from page_pair_calls import PageCalls
    monkeypatch.setattr(PageCalls,'storage_contract','unsupported',raising=False)
    with pytest.raises(ContractError,match='PAGE_PROTOCOL_UNSUPPORTED'):
        preflight({'page_execution_contract':CONTRACT,'page_budget_contract':BUDGET})

def test_missing_dispatch_cannot_enter_legacy_generator(tmp_path,monkeypatch):
    from types import SimpleNamespace as NS
    import fixed_page_work,reduce_coverage
    monkeypatch.setattr(reduce_coverage,'current',lambda *_:None)
    adapter=NS(workflow=NS(vault=tmp_path))
    with pytest.raises(ContractError,match='PAGE_PROTOCOL_UNSUPPORTED'):
        fixed_page_work.execute(adapter,{'workflow_id':'future'},{'view':{'page_execution_contract':CONTRACT}},
            {'pages':[]},None,lambda *_:pytest.fail('legacy called'),None,None)


def test_installed_protocol_preflight_checks_the_complete_chain():
    preflight({'page_execution_contract':CONTRACT,'page_budget_contract':BUDGET})


def test_missing_receipt_capability_holds_before_consumption(monkeypatch):
    from page_pair_calls import PageCalls
    monkeypatch.setattr(PageCalls,'settle',None)
    with pytest.raises(ContractError,match='PAGE_PROTOCOL_UNSUPPORTED'):
        preflight({'page_execution_contract':CONTRACT,'page_budget_contract':BUDGET})


def test_unknown_version_cannot_silently_fall_back():
    from page_protocol import strict
    with pytest.raises(ContractError,match='PAGE_PROTOCOL_UNSUPPORTED'):
        strict({'page_execution_contract':'future-unknown'})


def test_core_creation_pins_before_any_dispatch_and_rejects_missing_protocol(vault):
    import json
    from hermes_source_units.ingest_workflow import FileIngestWorkflowService,mutation_digest
    service=FileIngestWorkflowService(vault)
    q={'workflow_id':'ingest-isolated-core-protocol','actor':'agent','expected_revision':0,
        'profile':'compact-3','scope':{'source_paths':[],'knowledge_selector':'all-current'}}
    q['input_digest']=mutation_digest(q)
    w=service.start(q)
    path=vault/f"_system/ledgers/ingest-workflows/{w['workflow_id']}/presentation-contract.json"
    record=json.loads(path.read_text())
    assert w['page_execution_contract']==record['page_execution_contract']==CONTRACT
    assert record['page_budget_contract']==BUDGET and w['state']=='created'
    assert service.start(q)==w
    record.pop('page_execution_contract');path.write_text(json.dumps(record))
    with pytest.raises(ContractError,match='PAGE_PROTOCOL_UNSUPPORTED'):service.start(q)


def test_same_complete_builder_measures_single_and_pair_without_evidence_loss():
    from page_request import prepare
    from fixed_semantic import prepare_request_projection
    from fixed_page_evidence import restore_pair
    from fixed_semantic_recovery import _request_text
    import json
    rows=views()
    for row in rows:row[0]['identity']=row[1]['identity']
    for group in ([rows[0]],rows):
        view,sc,prepared,_=prepare(group,prepare_request_projection)
        body=json.loads(_request_text(prepared['wire']['user']))['input']
        if body.get('evidence_encoding')=='lossless-page-pair/v1':body=restore_pair(body)
        assert body['original_evidence']==rows[0][1]['original_evidence']
        assert len(body['pages'])==len(group)
        assert 'Write only the assigned candidates' in prepared['wire']['system']
        assert all(p['candidates'][0]['evidence_qa'][0]['qa_note'] for p in body['pages'].values())
