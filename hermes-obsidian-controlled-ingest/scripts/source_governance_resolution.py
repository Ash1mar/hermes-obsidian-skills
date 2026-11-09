#!/usr/bin/env python3
"""Explicit operator-authorized source binding resolution, approval and activation.

Only fixed declarations in hash-verified registered material or existing named
bindings establish identity. Directory names and model guesses are not evidence.
"""
import copy
import hashlib
import json
import os
import re
import sys
from contextlib import closing, contextmanager
from pathlib import Path

from governance_repository import GovernanceError, atomic_write_json, load_json, utc_now, safe_control_path
from hermes_source_units import FileSourceUnitService
from hermes_source_units.source_units import ARTIFACT_ROOT, _vault_path
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_guard import workflow_write_guard


@contextmanager
def operator_guard(repo,request):
    """Separate stopped-operator authority; never impersonate a source worker."""
    if not request.get('workflow_ref'):
        with workflow_write_guard(repo.vault,actor=request['actor']) as worker:yield worker
        return
    from hermes_source_units.ingest_workflow import FileIngestWorkflowService,TERMINAL,WORKFLOW_ROOT
    from hermes_source_units.source_units import _exclusive_lock
    service=FileIngestWorkflowService(repo.vault);wid=request['workflow_id']
    if safe_control_path(repo.vault,request['workflow_ref'],'workflow')!=service._path(wid):raise GovernanceError('Canonical workflow reference required')
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'hermes-obsidian-governed-ingest-orchestrator/lib'))
    from ingest_kanban import IngestKanbanAdapter
    from native_stop_inventory import verify_cancel_drain
    lock=_vault_path(repo.vault,f'{WORKFLOW_ROOT}/.dispatch-locks/{wid}.lock')
    with _exclusive_lock(lock),_exclusive_lock(service._lock(wid)):
        from hermes_source_units.recovery_control import check
        check(repo.vault,wid)
        w=service.status(wid);service.assert_execution_ready(w)
        if request.get('source_boundary') == 'post-mineru-short/v1':
            from native_stop_inventory import workflow_processes
            pause = w.get('pause_control', {})
            startup = load_json(_vault_path(repo.vault, w['scope']['bootstrap_ref']))
            pointer = startup['test_bundle_import']
            imported = _vault_path(repo.vault, pointer['ref'])
            report = load_json(imported)
            ready = {o['resource_id'] for o in w['source_outcomes'] if o['status'] == 'ready'}
            if (w['actor'] != request['actor'] or w['revision'] != request.get('expected_workflow_revision')
                    or w['scope'].get('pause_after') != ['exact_plan'] or pause.get('boundary') != 'exact_plan'
                    or pause.get('evidence_digest') != request.get('pause_digest') or w.get('release_id')
                    or startup['workflow_id'] != wid or report['workflow_id'] != wid
                    or pointer['contract'] != 'post-mineru-test/v1' or report['contract'] != 'post-mineru-test/v1'
                    or hashlib.sha256(imported.read_bytes()).hexdigest() != pointer['sha256']
                    or report['mineru_calls'] != 0 or report['semantic_results_imported'] is not False
                    or set(request['resource_ids']) != ready
                    or any(path != service._path(wid) for path in (repo.vault / WORKFLOW_ROOT).glob('*.json'))
                    or workflow_processes(wid, w['kanban']['board_id'])):
                raise GovernanceError('Requires the exact drained independent short-path source boundary')
            from hermes_cli import kanban_db, kanban_db_connect
            with closing(kanban_db_connect.connect(board=w['kanban']['board_id'])) as conn:
                if any(t.claim_lock or t.status == 'running' for t in kanban_db.list_tasks(conn)):
                    raise GovernanceError('Independent source boundary still has native claims')
            yield None
            return
        if (w['actor']!=request['actor'] or w['revision']!=request.get('expected_workflow_revision')
                or not w['cancel_requested'] or w['state']!='cancelled' or w['current_stage']!='release_planning'
                or service.pending_pause(w) or w.get('release_id')):raise GovernanceError('Requires exact drained stopped unbound release workflow')
        verify_cancel_drain(IngestKanbanAdapter(repo.vault),wid,timeout=0)
        for path in (repo.vault/WORKFLOW_ROOT).glob('*.json'):
            other=load_json(path)
            if other.get('workflow_id')!=wid and other.get('kanban',{}).get('task_map'):
                if other.get('state')=='cancelled' and other.get('cancel_requested'):
                    verify_cancel_drain(IngestKanbanAdapter(repo.vault),other['workflow_id'],timeout=0)
                elif other.get('state') not in TERMINAL:
                    raise GovernanceError('Another workflow can consume this governance state')
        yield None


def digest(value):
    return 'sha256:'+fingerprint(value)


def placeholder(org):
    return not org or org['name'].casefold().startswith('unresolved source organization')


def declarations(text):
    """Parse closed, role-labelled statements; never treat mentioned names as owners."""
    front=text.split('<!-- source-page: 5 -->')[0][:12000]
    first=front.split('<!-- source-page: 2 -->')[0]
    # Cover joint issuers can be rendered as separate lines or one merged line.
    if '联合发布' in first:
        names=re.findall(r'中华人民共和国[\u4e00-\u9fff]{2,35}?(?:部|总局)(?=\s|联合发布|中华人民共和国|$)',first)
        if len(set(names))>=2:return [{'name':n,'role':'issuer'} for n in sorted(set(names))]
        return []
    named=re.findall(r'([\u4e00-\u9fff]{2,30}(?:局|部|委员会))\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日批准发布',front)
    if len(set(named))==1:return [{'name':named[0],'role':'issuer'}]
    drafted=re.findall(r'^本标准起草单位[：:]([^\n]+)',front,re.M)
    if len(drafted)==1:
        names=[s.strip(' 。；;') for s in re.split('[、，,]',drafted[0])]
        if names and all(re.fullmatch(r'[\u4e00-\u9fffA-Za-z（）()]{3,60}',n) for n in names):
            return [{'name':n,'role':'drafter'} for n in names]
    owners=re.findall(r'^#\s+([\u4e00-\u9fff]{3,50}有限公司)\s*$',first,re.M)
    if len(set(owners))==1:return [{'name':owners[0],'role':'document_owner'}]
    return []


def inventory(repo, request, organizations, registry):
    service=FileSourceUnitService(repo.vault)
    workflow=load_json(safe_control_path(repo.vault,request['workflow_ref'],'workflow'))
    if workflow['actor']!=request['actor'] or workflow['workflow_id']!=request['workflow_id']:
        raise GovernanceError('Workflow identity/actor differs')
    outcomes={o['resource_id']:o for o in workflow['source_outcomes'] if o['status']=='ready'}
    selected=request['resource_ids']
    if not selected or len(set(selected))!=len(selected) or not set(selected)<=set(outcomes):
        raise GovernanceError('Only unique ready sources in the authorized workflow may resolve')
    by_id={o['id']:o for o in organizations['organizations']}
    labels={label.casefold():o['id'] for o in organizations['organizations'] if not placeholder(o)
            for label in [o['name'],*o['aliases']]}
    rows=[]
    for resource in selected:
        records=[r for r in registry['records'] if r['resource_id']==resource]
        if len(records)!=1:raise GovernanceError('Resource identity is not unique')
        record=records[0];outcome=outcomes[resource]
        if record['content_sha256']!=outcome['content_sha256']:raise GovernanceError('Workflow source hash differs')
        raw=_vault_path(repo.vault,record['storage_uri'].removeprefix('local://'))
        if not record['storage_uri'].startswith('local://') or hashlib.sha256(raw.read_bytes()).hexdigest()!=record['content_sha256']:
            raise GovernanceError('Registered raw source hash differs')
        service.validate(resource,outcome['unit_set_id'])
        current=service._current(resource)
        if current['unit_set_id']!=outcome['unit_set_id']:raise GovernanceError('Current source differs from workflow')
        path=_vault_path(repo.vault,f"{ARTIFACT_ROOT}/{resource}/{current['artifact_revision']}/manifest.json")
        artifact,text,_=service._verify_artifact(path)
        if artifact['identity']['version_id']!=record['version_id']:raise GovernanceError('Artifact version differs')
        if artifact['source_sha256']!=record['content_sha256']:raise GovernanceError('Artifact source hash differs')
        evidence={'artifact_ref':path.relative_to(repo.vault).as_posix(),'artifact_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                  'document_sha256':artifact['document_sha256'],'source_sha256':record['content_sha256']}
        mappings=[];reason=None
        for occurrence in record['source_occurrences']:
            org=by_id.get(occurrence['source_organization_id'])
            names=declarations(text) if placeholder(org) else [{'name':org['name'],'role':occurrence.get('organization_role','registered_source')}]
            if not names:reason='no-unique-role-labelled-identity';break
            resolved=[]
            for item in names:
                oid=labels.get(item['name'].casefold()) or 'organization-'+hashlib.sha256(item['name'].encode()).hexdigest()[:24]
                if by_id.get(oid,{}).get('status')=='retired':reason='organization-retired';break
                resolved.append({**item,'organization_id':oid})
            mappings.append({'original':occurrence,'resolved':resolved})
        others=[r for r in registry['records'] if r['document_id']==record['document_id'] and r['governance_status']=='active' and r['version_id']!=record['version_id']]
        if record['processing_status']!='completed' or record['governance_status'] in ('withdrawn','superseded'):reason='version-not-activatable'
        if others and (len(others)!=1 or record.get('supersedes_version_id')!=others[0]['version_id']):reason='version-relationship-unresolved'
        rows.append({'resource_id':resource,'version_id':record['version_id'],'resolved':reason is None,
                     'reason':reason,'mappings':mappings,'evidence':evidence})
    return rows


def resolve(repo, request, *, dry_run=False):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise GovernanceError('Only the trusted authorized operator may resolve governance')
    body={k:v for k,v in request.items() if k!='input_digest'}
    if request.get('input_digest')!=digest(body) or request.get('automatic_approval_activation') is not True:
        raise GovernanceError('Exact authorized request and automatic approval scope required')
    operation=request.get('operation_id','')
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation) or not request.get('reason','').strip():
        raise GovernanceError('Stable operation identity and reason required')
    root=repo.vault/'_system/metadata/source-governance-resolutions'/operation
    txn_path=root/'transaction.json';receipt=root/'receipt.json'
    with operator_guard(repo,request) as worker,repo.mutation_lock(request['actor']):
        if worker:raise GovernanceError('Worker cannot approve source governance')
        if txn_path.exists():
            txn=load_json(txn_path)
            if txn['request']!=request:raise GovernanceError('Operation identity conflict')
            if receipt.exists():return {**load_json(receipt),'replayed':True}
        else:
            orgs,registry=repo.load_state();repo._validate_or_raise(orgs,registry)
            if (orgs['registry_revision'],registry['registry_revision'])!=(request['expected_organization_revision'],request['expected_registry_revision']):
                raise GovernanceError('Governance revision conflict')
            rows=inventory(repo,request,orgs,registry)
            result={'ok':True,'resolved':sum(r['resolved'] for r in rows),'unresolved':[r for r in rows if not r['resolved']],
                    'rows':rows,'model_calls':0,'resumed':False}
            if dry_run:return {**result,'applied':False,'preview_digest':digest(rows)}
            if request.get('preview_digest')!=digest(rows):raise GovernanceError('Resolution preview/evidence changed')
            next_orgs=copy.deepcopy(orgs);next_registry=copy.deepcopy(registry);now=utc_now()
            ids={o['id']:o for o in next_orgs['organizations']};changes=[]
            for row in rows:
                if not row['resolved']:continue
                record=next(r for r in next_registry['records'] if r['version_id']==row['version_id'])
                updated=[]
                for mapping in row['mappings']:
                    for index,item in enumerate(mapping['resolved']):
                        oid=item['organization_id']
                        if oid not in ids:
                            ids[oid]={'id':oid,'name':item['name'],'aliases':[],'status':'candidate'};next_orgs['organizations'].append(ids[oid])
                        ids[oid]['status']='approved'
                        source=copy.deepcopy(mapping['original']);source['source_organization_id']=oid;source['organization_role']=item['role']
                        if index:source['source_occurrence_id']='occurrence-'+fingerprint([mapping['original']['source_occurrence_id'],oid])[:40]
                        updated.append(source)
                record['source_occurrences']=updated;record['governance_status']='active';record['updated_at']=now
                for old in next_registry['records']:
                    if old['version_id']==record.get('supersedes_version_id') and old['governance_status']=='active':old['governance_status']='superseded';old['updated_at']=now
                changes.append({'version_id':record['version_id'],'old_occurrences':[m['original'] for m in row['mappings']],
                                'new_occurrences':updated,'evidence':row['evidence']})
            for target,before in [(next_orgs,orgs),(next_registry,registry)]:
                if target!=before:
                    target['registry_revision']+=1
                    target['events'].append(repo._event('authorized_source_governance_resolved',request['actor'],target['registry_revision'],
                        operation_id=operation,request_digest=request['input_digest'],changes=changes))
            repo._validate_or_raise(next_orgs,next_registry)
            txn={'request':request,'before':[orgs,registry],'after':[next_orgs,next_registry],'result':result}
            atomic_write_json(txn_path,txn)
        # A two-file journal allows exact crash recovery; never overwrite foreign drift.
        for path,before,after in zip([repo.organizations_path,repo.registry_path],txn['before'],txn['after']):
            current=load_json(path)
            if current not in (before,after):raise GovernanceError('Governance changed during pending transaction')
        # Reauthenticate originals and normalized inputs even after an interrupted commit.
        for row in txn['result']['rows']:
            p=safe_control_path(repo.vault,row['evidence']['artifact_ref'],'artifact')
            if hashlib.sha256(p.read_bytes()).hexdigest()!=row['evidence']['artifact_sha256']:raise GovernanceError('Resolution evidence changed')
            rec=next(r for r in txn['before'][1]['records'] if r['version_id']==row['version_id'])
            if hashlib.sha256(_vault_path(repo.vault,rec['storage_uri'][8:]).read_bytes()).hexdigest()!=rec['content_sha256']:raise GovernanceError('Raw source changed')
            artifact,text,_=FileSourceUnitService(repo.vault)._verify_artifact(p)
        for path,after in zip([repo.organizations_path,repo.registry_path],txn['after']):
            if load_json(path)!=after:atomic_write_json(path,after)
        result={**txn['result'],'applied':True,'operation_id':operation,'receipt_ref':receipt.relative_to(repo.vault).as_posix()}
        atomic_write_json(receipt,result);return result
