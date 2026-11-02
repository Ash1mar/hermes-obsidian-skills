"""Isolated governance identity, crash-recovery and non-guessing checks."""
import copy,hashlib,json,sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_document_governance import create_engineering_vault,organization_add,register_version
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hermes-obsidian-controlled-ingest/scripts'))
import source_governance_resolution as resolution
from governance_repository import JsonGovernanceRepository,GovernanceError

@pytest.mark.parametrize('text,role,count',[
 ('中华人民共和国测试部\n中华人民共和国测试总局\n联合发布','issuer',2),
 ('中华人民共和国测试部联合发布中华人民共和国测试总局','issuer',2),
 ('(国家测试局 2019 年 12 月 31 日批准发布)','issuer',1),
 ('本标准起草单位：甲方工程有限公司、乙方研究设计院。','drafter',2),
 ('# 测试工程有限公司\nCompany','document_owner',1),
])
def test_roles_are_parsed_from_declarations(text,role,count):
    found=resolution.declarations(text)
    assert len(found)==count and {r['role'] for r in found}=={role}

@pytest.mark.parametrize('text',['参考文件由某公司编制','CNPE/项目/文件.pdf','联合发布','# 某文件\n提及甲公司、乙公司'])
def test_mentions_and_paths_never_establish_identity(text):assert resolution.declarations(text)==[]

@pytest.fixture
def fixture(tmp_path,monkeypatch):
    vault=create_engineering_vault(tmp_path);organization_add(vault)
    raw=vault/'10_Raw/source-a.pdf';raw.write_bytes(b'original')
    source_hash=hashlib.sha256(raw.read_bytes()).hexdigest();register_version(vault,'a',0,source_hash)
    repo=JsonGovernanceRepository(vault);orgs,registry=repo.load_state();record=registry['records'][0]
    artifact=vault/'_system/artifact.json';artifact.write_text('{}')
    row={'resource_id':record['resource_id'],'version_id':record['version_id'],'resolved':True,'reason':None,
     'mappings':[{'original':record['source_occurrences'][0],'resolved':[{'organization_id':'organization-owner','name':'Owner Organization','role':'registered_source'}]}],
     'evidence':{'artifact_ref':artifact.relative_to(vault).as_posix(),'artifact_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),
                 'document_sha256':'x','source_sha256':source_hash}}
    monkeypatch.setattr(resolution,'inventory',lambda *a:[copy.deepcopy(row)])
    monkeypatch.setattr(resolution,'FileSourceUnitService',lambda *a:SimpleNamespace(_verify_artifact=lambda *a:({},'',None)))
    q={'workflow_id':'ingest-fixture','actor':'test-suite','operation_id':'resolution-test','reason':'Authorized isolated governance resolution',
       'automatic_approval_activation':True,'expected_organization_revision':orgs['registry_revision'],'expected_registry_revision':registry['registry_revision']}
    q['input_digest']=resolution.digest(q);preview=repo.resolve_sources(q,dry_run=True)
    q['preview_digest']=preview['preview_digest'];q['input_digest']=resolution.digest({k:v for k,v in q.items() if k!='input_digest'})
    return repo,q,raw,artifact

def test_resolution_approves_only_selected_binding_and_replays_without_changes(fixture):
    repo,q,raw,artifact=fixture;first=repo.resolve_sources(q);before=[p.read_bytes() for p in [repo.organizations_path,repo.registry_path]]
    assert first['resolved']==1 and repo.resolve_sources(q)['replayed']
    assert before==[p.read_bytes() for p in [repo.organizations_path,repo.registry_path]]
    orgs,registry=repo.load_state();assert orgs['organizations'][0]['status']=='approved' and registry['records'][0]['governance_status']=='active'
    assert registry['records'][0]['authority_status']=='official'
    assert registry['events'][-1]['changes'][0]['old_occurrences']

def test_interrupted_two_file_transaction_recovers_exactly(fixture,monkeypatch):
    repo,q,raw,artifact=fixture;write=resolution.atomic_write_json
    def fail(path,value):
        if path==repo.registry_path:raise OSError('simulated interrupted second write')
        write(path,value)
    monkeypatch.setattr(resolution,'atomic_write_json',fail)
    with pytest.raises(OSError):repo.resolve_sources(q)
    monkeypatch.setattr(resolution,'atomic_write_json',write)
    assert repo.resolve_sources(q)['resolved']==1

def test_pending_transaction_rejects_changed_original(fixture,monkeypatch):
    repo,q,raw,artifact=fixture;write=resolution.atomic_write_json
    def fail(path,value):
        if path==repo.organizations_path:raise OSError('interrupted')
        write(path,value)
    monkeypatch.setattr(resolution,'atomic_write_json',fail)
    with pytest.raises(OSError):repo.resolve_sources(q)
    monkeypatch.setattr(resolution,'atomic_write_json',write);raw.write_bytes(b'changed')
    with pytest.raises(GovernanceError,match='Raw source changed'):repo.resolve_sources(q)

def test_worker_cannot_approve_and_stale_revision_cannot_mutate(fixture,monkeypatch):
    repo,q,_,_=fixture;monkeypatch.setenv('HERMES_KANBAN_TASK','worker')
    with pytest.raises(GovernanceError):repo.resolve_sources(q)
    monkeypatch.delenv('HERMES_KANBAN_TASK');q['expected_registry_revision']=999
    q['input_digest']=resolution.digest({k:v for k,v in q.items() if k!='input_digest'})
    with pytest.raises(GovernanceError,match='revision conflict'):repo.resolve_sources(q)

def test_stopped_operator_guard_checks_other_cancelled_workflows_and_rejects_active(tmp_path,monkeypatch):
    import hermes_source_units.ingest_workflow as workflow
    import ingest_kanban,native_stop_inventory
    root=tmp_path/'_system/ledgers/ingest-workflows';root.mkdir(parents=True)
    w={'workflow_id':'ingest-current','actor':'operator','revision':7,'state':'cancelled','cancel_requested':True,'current_stage':'release_planning','release_id':None}
    other={'workflow_id':'ingest-history','state':'cancelled','cancel_requested':True,'kanban':{'task_map':[{}]}}
    path=root/'ingest-history.json';path.write_text(json.dumps(other))
    service=SimpleNamespace(_path=lambda wid:root/(wid+'.json'),_lock=lambda wid:root/(wid+'.lock'),
        status=lambda wid:w,assert_execution_ready=lambda w:None,pending_pause=lambda w:False)
    monkeypatch.setattr(workflow,'FileIngestWorkflowService',lambda *a:service)
    monkeypatch.setattr(ingest_kanban,'IngestKanbanAdapter',lambda *a:None)
    observed=[];monkeypatch.setattr(native_stop_inventory,'verify_cancel_drain',lambda a,wid,**kw:observed.append(wid))
    q={'workflow_id':'ingest-current','actor':'operator','workflow_ref':'_system/ledgers/ingest-workflows/ingest-current.json','expected_workflow_revision':7}
    with resolution.operator_guard(SimpleNamespace(vault=tmp_path),q):pass
    assert observed==['ingest-current','ingest-history']
    other.update(state='release_planning',cancel_requested=False);path.write_text(json.dumps(other))
    with pytest.raises(GovernanceError,match='Another workflow'): 
        with resolution.operator_guard(SimpleNamespace(vault=tmp_path),q):pass
