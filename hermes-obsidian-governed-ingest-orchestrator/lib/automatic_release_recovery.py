"""Initial internal release qualification under the scoped short-request intent.

Closed source declarations use the existing governance operation. Native release
planning qualifies exact committed acceptance; QA is retained and no model runs.
"""
import hashlib,sys
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json,_vault_path,_write_atomic,_json_bytes,_exclusive_lock
from hermes_source_units.validation import fingerprint


def recover(adapter,wid,*,resume=True):
    from automatic_pass_recovery import request,enabled
    from native_ingest import control_workflow
    from native_stop_inventory import verify_cancel_drain
    from hermes_source_units.vault_finalize import FileVaultFinalizeService
    from hermes_source_units.partial_finalize import pin
    from hermes_source_units.page_governance import POLICY
    from hermes_source_units.initial_release import CONTRACT,admitted_pointer,needs_preflight
    service=adapter.workflow;root=service.vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-release'
    with _exclusive_lock(root/'.controller.lock'):
        w=service.status(wid)
        proactive=needs_preflight(service.vault,w)
        if (not enabled(w) and not proactive) or w['current_stage']!='release_planning' or service.pending_pause(w) or w.get('release_id'):return False
        if w['scope'].get('recovery_policy',{}).get('initial_release')=='hold':return False
        bootstrap=w['scope'].get('bootstrap_ref')
        if not bootstrap:return False
        start=_load_json(_vault_path(service.vault,bootstrap))
        if start.get('workflow_id')!=wid or start.get('contract')!='vault-startup-report/v1':raise ContractError('STALE_INPUT','$','initial release requires the exact Bootstrap intent')
        card=next((c for c in w['kanban']['task_map'] if c['node']=='vault-finalize-plan'),None)
        admitted=admitted_pointer(service.vault,w)
        if admitted:
            # A crash after admission but before resume replays the same stable
            # operation. A newly failed plan cannot mint another qualification.
            from hermes_source_units.partial_finalize import pinned
            report=pinned(service.vault,{'ref':w['release_recovery_history'][-1]['report_ref'],
                'sha256':w['release_recovery_history'][-1]['report_sha256']})
            if card and card['task_id']==report['previous_task_id']:
                if resume and w['cancel_requested']:
                    op=report['request']['recovery_id']
                    control_workflow(service.vault,wid,'resume',op+'-resume')
                return True
            return False
        fp=service.vault/f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint("vault-finalize-plan")[:16]}.json'
        if not card:return False
        if proactive:
            from ingest_kanban import desired_graph,domain_completed
            nodes={n.name:n for n in desired_graph(service,w)}
            if not domain_completed(service,w,nodes['build-finalize']):return False
            node=nodes['vault-finalize-plan']
            if adapter.kanban.task_snapshot_fresh(w['kanban']['board_id'],card['task_id']).get('runs'):
                raise ContractError('STALE_INPUT','$','initial preflight requires an unstarted native plan')
            failure={'input_fingerprint':node.input_fingerprint}
        else:
            if not fp.exists():return False
            failure=_load_json(fp)
            if failure.get('task_id')!=card['task_id'] or failure.get('error_code')!='STALE_INPUT':return False
        p4=FileVaultFinalizeService(service.vault)
        if (p4._state().get('current_release_id') or start.get('vault_id')!=p4.source.vault_manifest['vault']['id']):return False
        release='release-'+fingerprint({'workflow_id':wid,'node':card['node'],'input_fingerprint':failure['input_fingerprint']})[:24]
        pp=p4._plan_path(release)
        op='auto-release-'+fingerprint(card['task_id'])[:20]
        if not w['cancel_requested']:control_workflow(service.vault,wid,'cancel',op+'-stop')
        verify_cancel_drain(adapter,wid,timeout=0);w=service.status(wid)
        if proactive and not pp.exists():
            # Deterministic preview before granting the first native planning run.
            # It is audit evidence, not a failed task or a release application.
            from hermes_source_units.delivery_scope import closure_refs
            batch=service.knowledge._batch(w['batch_id']);delivery=closure_refs(service.knowledge,batch['run_ids'])
            p4.plan({'actor':w['actor'],'release_id':release,'expected_state_revision':p4._state()['revision'],
                'build_run_ids':batch['run_ids'],**({'delivery_scopes':delivery} if delivery else {})})
        plan=_load_json(pp)
        if plan.get('blockers')!=['no-deliverable-pages']:return False
        # Exact registered content and role-labelled declarations establish source
        # identity. The program never guesses owners from filenames or directories.
        scripts=Path(__file__).resolve().parents[2]/'hermes-obsidian-controlled-ingest/scripts';sys.path.insert(0,str(scripts))
        from governance_repository import JsonGovernanceRepository
        from source_governance_resolution import digest
        repo=JsonGovernanceRepository(service.vault);gov_path=root/(op+'-source.request.json')
        if not gov_path.exists():
            orgs,registry=repo.load_state()
            q={'workflow_id':wid,'workflow_ref':service._path(wid).relative_to(service.vault).as_posix(),
                'actor':w['actor'],'operation_id':op+'-source','reason':'Authorized short-request internal Vault release and Provider scope; deterministic source declarations only',
                'automatic_approval_activation':True,'expected_workflow_revision':w['revision'],
                'expected_organization_revision':orgs['registry_revision'],'expected_registry_revision':registry['registry_revision'],
                'resource_ids':sorted(o['resource_id'] for o in w['source_outcomes'] if o['status']=='ready')}
            q['input_digest']=digest(q);preview=repo.resolve_sources(q,dry_run=True)
            _write_atomic(root/(op+'-source-disposition.json'),_json_bytes({
                'contract':'automatic-initial-source-disposition/v1','workflow_id':wid,
                'resolved_resources':[r['resource_id'] for r in preview['rows'] if r['resolved']],
                'isolated_resources':preview['unresolved'],'dependent_action':'exclude pages without eligible source support',
                'new_model_calls':0,'human_review_requested':False,'identity_guessed':False}))
            if not preview['resolved']:return False
            q['preview_digest']=preview['preview_digest'];q['input_digest']=digest({k:v for k,v in q.items() if k!='input_digest'})
            _write_atomic(gov_path,_json_bytes(q))
        resolution=repo.resolve_sources(_load_json(gov_path))
        receipt=pin(service.vault,f'_system/metadata/source-governance-resolutions/{op}-source/receipt.json')
        grant_path=root/(op+'-page-authorization.json')
        if not grant_path.exists():
            q={'workflow_id':wid,'actor':w['actor'],'operation_id':op+'-initial-page',
                'policy':POLICY,'automatic_page_approval':True,'stop_after':'release_apply',
                'retrieval_scope':p4.source.vault_manifest['vault']['id'],'base_release':pin(service.vault,pp.relative_to(service.vault).as_posix()),
                'basis_kind':'initial_draft_plan','bootstrap':pin(service.vault,bootstrap),'source_governance_receipt':receipt,
                'source_governance_transaction':pin(service.vault,f'_system/metadata/source-governance-resolutions/{op}-source/transaction.json'),
                'human_review_performed':False}
            q['input_digest']='sha256:'+fingerprint(q)
            _write_atomic(grant_path,_json_bytes({'contract':CONTRACT,'request':q,
                'preserve_domain_results':True,'new_model_calls':0,'resumed':False}))
        recovery=root/(op+'-recovery.request.json')
        if not recovery.exists():
            q=request(adapter,wid,recovery_id=op,recovery_scope='initial-release-preflight/v1' if proactive else 'governance-resolved/v1',
                reason='Program initial internal source/page qualification; scoped to the authorized Bootstrap workflow',
                task_id=card['task_id'],input_fingerprint=failure['input_fingerprint'],
                **({} if proactive else {'failure_sha256':hashlib.sha256(fp.read_bytes()).hexdigest()}),blocked_plan=pin(service.vault,pp.relative_to(service.vault).as_posix()),
                governance_receipt=receipt,initial_page_governance=pin(service.vault,grant_path.relative_to(service.vault).as_posix()))
            _write_atomic(recovery,_json_bytes(q))
        result=adapter.recover_release_execution(_load_json(recovery));_write_atomic(root/(op+'-receipt.json'),_json_bytes(result))
        if resume:control_workflow(service.vault,wid,'resume',op+'-resume')
        return True
