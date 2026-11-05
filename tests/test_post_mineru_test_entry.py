"""Isolated imported conversion material; no model, MinerU or real workflow."""
import copy,json
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from hermes_source_units import ContractError
from hermes_source_units.source_units import _write_atomic,_json_bytes
from post_mineru_test import import_bundles,imported_bundle,inventory,sha,CONTRACT
from test_manage_bundle_ingest import BundleLedgerTest

@pytest.fixture
def inputs(tmp_path,monkeypatch):
    origin=tmp_path/'origin';target=tmp_path/'target';base=origin/'_system/reports/source-bundles';base.mkdir(parents=True)
    bundle=BundleLedgerTest().create_bundle(base)
    raw=tmp_path/'sample.pdf';raw.write_bytes(b'isolated original');digest=sha(raw.read_bytes())
    m=json.loads((bundle/'manifest.json').read_text());m['source']['sha256']=digest
    m['quality']={'status':'warn','issues':[{'code':'fixture','severity':'warn','message':'Keep QA'}],'review_required':['table review']}
    (bundle/'manifest.json').write_text(json.dumps(m))
    rel=bundle.relative_to(origin).as_posix()
    w={'state':'partial','source_outcomes':[{'path':'10_Raw/sample.pdf','status':'ready','content_sha256':digest,
        'artifact_refs':[rel+'/manifest.json']}],'kanban':{'board_id':'isolated'}}
    _write_atomic(origin/'_system/ledgers/ingest-workflows/old.json',_json_bytes(w))
    _write_atomic(target/'10_Raw/sample.pdf',raw.read_bytes())
    _write_atomic(target/'_system/vault.json',_json_bytes({'vault':{'id':'new'}}))
    q={'test_mode':CONTRACT,'operation_id':'isolated','vault_id':'new','sources':[str(raw)],
       'reuse_conversion':{'vault':str(origin),'workflow_id':'old','sources':[{'filename':'sample.pdf','source_sha256':digest,'bundle_ref':rel,'files':inventory(bundle)}]}}
    monkeypatch.setattr('native_stop_inventory.workflow_processes',lambda *_:[])
    return origin,target,bundle,q,m

def test_import_is_lossless_except_new_binding_and_idempotent(inputs):
    origin,target,bundle,q,original=inputs;before=inventory(bundle)
    p=import_bundles(target,q);receipt=json.loads((target/p['ref']).read_text());row=receipt['sources'][0]
    new=json.loads((target/row['bundle_ref']/'manifest.json').read_text())
    assert new['quality']==original['quality'] and new['source']['sha256']==original['source']['sha256']
    assert new['source']['path']==str(target/'10_Raw/sample.pdf') and 'governance' not in new
    assert inventory(bundle)==before and receipt['semantic_results_imported'] is False
    assert row['native_binding_pending'] and import_bundles(target,q)==p
    new['governance']={'vault_id':'new','document_id':'new-native-document','version_id':'new-native-version'}
    (target/row['bundle_ref']/'manifest.json').write_text(json.dumps(new))
    assert import_bundles(target,q)==p

@pytest.mark.parametrize('fault',['hash','source','membership','live_origin','mode'])
def test_bad_origin_never_imports_or_converts(inputs,monkeypatch,fault):
    origin,target,bundle,q,_=inputs
    if fault=='hash':(bundle/'document.md').write_text('changed')
    if fault=='source':q['reuse_conversion']['sources'][0]['source_sha256']='f'*64
    if fault=='membership':(bundle/'unexpected.md').write_text('extra')
    if fault=='live_origin':monkeypatch.setattr('native_stop_inventory.workflow_processes',lambda *_:[{'pid':7}])
    if fault=='mode':q.pop('test_mode')
    with pytest.raises(ContractError):import_bundles(target,q)
    assert not (target/'_system/reports/source-bundles').exists()

def test_native_worker_consumes_only_exact_receipt_source_and_rejects_drift(inputs):
    _,target,_,q,_=inputs;p=import_bundles(target,q)
    report='_system/reports/start.json';_write_atomic(target/report,_json_bytes({'test_bundle_import':p}))
    w={'scope':{'bootstrap_ref':report}};a=NS(workflow=NS(vault=target,status=lambda _:w));b={'workflow_id':'ingest-isolated'}
    source={'path':'10_Raw/sample.pdf','content_sha256':q['reuse_conversion']['sources'][0]['source_sha256']}
    ref=imported_bundle(a,b,source);assert ref.startswith('_system/reports/source-bundles/')
    (target/ref/'document.md').write_text('changed after import')
    with pytest.raises(ContractError,match='changed'):imported_bundle(a,b,source)

def test_ordinary_workflow_has_no_implicit_cross_vault_reuse(tmp_path):
    a=NS(workflow=NS(vault=tmp_path,status=lambda _:{'scope':{}}))
    assert imported_bundle(a,{'workflow_id':'ordinary'}, {}) is None

def test_short_entry_fixes_future_profile_before_any_sync(tmp_path,monkeypatch):
    import orchestration
    value={'workflow_id':'future','scope':{},'template_pins':[{}]}
    s=NS(vault=tmp_path,start=lambda _:value,pinned_templates=lambda _:None)
    monkeypatch.setattr(orchestration,'FileIngestWorkflowService',lambda _:s)
    orchestration.start_and_pin(tmp_path,{})
    pin=json.loads((tmp_path/'_system/ledgers/ingest-workflows/future/presentation-contract.json').read_text())
    assert pin['page_dispatch_contract']=='per-page-dispatch/v2' and pin['planning_budget_contract']=='shared-reading-page-budget/v1'

def test_full_start_wrapper_accepts_and_pins_test_callback(tmp_path,monkeypatch):
    from test_startup_unknown_fix import startup,ROOT
    from orchestration import start_and_pin
    module=startup();source=tmp_path/'source.md';source.write_text('isolated original')
    q={'operation_id':'callback','actor':'agent','vault_path':str(tmp_path/'new'),'vault_id':'new','vault_name':'New',
       'security_domain':'test','sources':[str(source)]}
    pointer={'contract':CONTRACT,'ref':'_system/reports/isolated-import.json','sha256':'a'*64};seen=[]
    def imported(v,r):
        assert (v/'10_Raw/source.md').read_text()=='isolated original'
        assert not (v/'_system/ledgers/ingest-workflows/ingest-callback.json').exists()
        seen.append('import');return pointer
    def dispatch(v,cmd,r):
        report=json.loads((v/r['scope']['bootstrap_ref']).read_text())
        assert report['test_bundle_import']==pointer and seen==['import']
        seen.append('dispatch');start_and_pin(v,r);return {'ok':True}
    monkeypatch.setattr(module,'dispatch',dispatch)
    result=module.start(q,tmp_path/'receipt.json',ROOT/'hermes-obsidian-vault-bootstrap',before_workflow=imported)
    assert result['workflow']['ok'] and seen==['import','dispatch']
