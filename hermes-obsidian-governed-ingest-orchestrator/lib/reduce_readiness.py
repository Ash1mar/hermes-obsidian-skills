"""Read-only, whole-batch mechanical inventory before any new Reduce dispatch."""
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.validation import fingerprint, record_validation_scope, validate_record
from hermes_source_units.reading_dependencies import CONTRACT, citation_dependency_gaps
from hermes_source_units.reduce_dependency_revision import snapshot


def global_budget(adapter,w):
    """Measure the currently available completed-source prefix, never a model call."""
    from fixed_reduce import preview_snapshot,global_plan_view
    from fixed_semantic import prepare_request_projection
    from fixed_semantic_schema import schema
    from fixed_semantic_media import image_catalog,select_images
    k=adapter.workflow.knowledge;batch=k._batch(w['batch_id'])
    if not batch['resource_reduction_ids']:return {'status':'not_yet_available','complete_input':False}
    card=next((c for c in w['kanban']['task_map'] if c['node']=='global-reduce'),None)
    if not card:return {'status':'binding_unavailable','complete_input':False}
    try:
        binding=_load_json(_vault_path(adapter.workflow.vault,
            f'_system/ledgers/ingest-workflows/{w["workflow_id"]}/bindings/{fingerprint(card["idempotency_key"])}.json'))
        value=preview_snapshot(adapter,binding);view=global_plan_view(value)
        phase='global-plan'
        size=prepare_request_projection(phase,view,schema(phase))['input_size']
        from fixed_global_packets import units,next_packet,initial
        remaining=units(value);packets=[];covered=[]
        while remaining:
            packet=next_packet(value,remaining,initial(),prepare_request_projection)
            packets.append({'proposal_refs':packet['proposal_refs'],'candidate_refs':packet['candidate_refs'],'input_codepoints':packet['input_codepoints']})
            covered.extend(packet['candidate_refs']);remaining=remaining[packet['unit_count']:]
        return {'status':'incremental_atoms_fit','phase':phase,'legacy_single_input_codepoints':size,'limit':30000,
            'available_completed_sources':len(batch['resource_reduction_ids']),'candidates':len(view['candidates']),
            'proposals':len(view['proposals']),'complete_input':False,'packets':packets,
            'covered_once':len(covered)==len(set(covered)) and set(covered)==set(value['backend']['mapping']),
            'note':'Mechanical empty-state capacity only; every live packet is remeasured with exact related prior planning state. No semantic plan or global readiness is inferred.'}

    except (ContractError,OSError,ValueError,KeyError) as exc:
        return {'status':'held','complete_input':False,'reason':str(exc)}


def scan(adapter,wid,*,budgets=True):
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise ContractError('ACCESS_DENIED','$','readiness requires a trusted stopped operator')
    service=adapter.workflow;w=service.status(wid);k=service.knowledge
    if w['state']!='cancelled' or w['current_stage']!='reducing':
        raise ContractError('INVALID_TRANSITION','$','inventory requires stopped Reduce')
    from native_stop_inventory import verify_cancel_drain
    drain=verify_cancel_drain(adapter,wid,timeout=0)
    batch=k._batch(w['batch_id']);rows=[];issues=[];sources={};completed={};slice_checks=[]
    initial={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (service._path(wid),k._batch_path(w['batch_id']))}
    def issue(scope,exc):
        issues.append({'scope':scope,'code':getattr(exc,'code','CHECK_FAILED'),'reason':str(exc)})
    with record_validation_scope(),service.execution_projection():
        k.source.enable_session_cache()
        service.verify_pass_skips(w)
        for rid in batch.get('resource_reduction_ids',[]):
            try:
                r=k._resource_reduction(batch['batch_id'],rid)
                completed[r['resource_id']]={'reduction_id':rid,'sha256':hashlib.sha256(k._resource_reduction_path(batch['batch_id'],rid).read_bytes()).hexdigest()}
                for t in r['tasks']:
                    if k._task(t['task_id'])['revision']!=t['revision']:
                        raise ContractError('STALE_INPUT','$','completed source task revision changed')
            except (ContractError,OSError,ValueError,KeyError) as exc:issue(rid,exc)
        for outcome in w['source_outcomes']:
            resource=outcome.get('resource_id')
            if resource:sources[resource]={'resource_id':resource,'source':outcome['path'],'tasks':[],
                'completed':completed.get(resource),'current_failure':None,'budget':{'status':'not_checked'}}
            try:
                if hashlib.sha256(_vault_path(service.vault,outcome['path']).read_bytes()).hexdigest()!=outcome['content_sha256']:
                    raise ContractError('SOURCE_CHANGED','$','source original hash changed')
            except (ContractError,OSError,ValueError,KeyError) as exc:issue(outcome['path'],exc)
        try:service.pinned_templates(w);service._verify_execution_evidence(w)
        except (ContractError,OSError,ValueError,KeyError) as exc:issue('execution_contract',exc)
        skipped=service.skipped_pass_tasks(w)
        for tid in batch['task_ids']:
            row={'task_id':tid,'errors':[],'gaps':[],'qa':[]};rows.append(row)
            try:
                task=k._task(tid);row.update(resources=sorted({r['unit_ref']['resource_id'] for r in task['target_refs']}),
                    status=task['status'],excluded=tid in skipped,target_refs=task['target_refs'])
                for resource in row['resources']:sources.setdefault(resource,{'resource_id':resource,'tasks':[],'completed':completed.get(resource),'current_failure':None,'budget':{'status':'not_checked'}})['tasks'].append(tid)
                k._live_units(task['target_refs'],w['actor'],batch['document_registry_revision'])
                k._verify_planned_measurement(task,w['actor'],batch['document_registry_revision'],None)
                candidates,by=k._pass_index([tid]);records=sorted(by.get(tid,[]),key=lambda p:p['sequence'])
                row['pass_count']=len(records)
                if not records or [p['sequence'] for p in records]!=list(range(len(records))):
                    raise ContractError('INCOMPLETE_PASSES','$','missing or noncontiguous Pass history')
                for r in records:
                    if fingerprint({f:v for f,v in r.items() if f!='pass_id'})!=r['pass_id']:
                        raise ContractError('SOURCE_CHANGED','$','Pass fingerprint changed')
                latest=records[-1];row.update(latest_pass_id=latest['pass_id'],latest_kind=latest['pass_kind'],
                    candidate_count=len(latest['candidates']),empty_reason=latest['empty_reason'])
                if latest['pass_kind']!='citation':raise ContractError('INCOMPLETE_PASSES','$','no terminal citation')
                package=_load_json(_vault_path(service.vault,f"_system/knowledge-builds/task-{tid}/readings/{latest['reading_package_id']}.json"))
                validate_record('reading_package',package);k._reading_package_fingerprint(package,latest['reading_package_id'])
                if package['actor']!=w['actor'] or package['task_id']!=tid or package['document_registry_revision']!=batch['document_registry_revision']:
                    raise ContractError('STALE_INPUT','$','reading authority differs')
                row['gaps']=citation_dependency_gaps(k.source,package,latest)
                row['qa']=[i for i in latest['inspections'] if i['qa']=='needs-qa']
                projection=k.model_input(tid,package,persist=False)
                for c in latest['candidates']:
                    for ref in c['support_refs']:k._model_handle(ref,projection['mapping'])
                for inspection in latest['inspections']:k._model_handle(inspection['source_ref'],projection['mapping'])
                row['rendered_mapping_valid']=True
            except (ContractError,OSError,ValueError,KeyError) as exc:
                detail={'code':getattr(exc,'code','CHECK_FAILED'),'reason':str(exc)}
                if row.get('excluded') and detail['code']=='INCOMPLETE_PASSES':
                    row['excluded_history_limitation']=detail
                else:row['errors'].append(detail)
        indexed={r['task_id']:r for r in rows}
        from hermes_source_units.legacy_slice_identity import creation_config
        for item in k._slices(batch['batch_id']):
            check={'slice_id':item['slice_id'],'task_ids':item['task_ids'],'state':item['state'],
                'requires_reopen':any(indexed[t]['gaps'] and indexed[t].get('candidate_count',0)>0 for t in item['task_ids'])}
            try:
                inputs=[{'task_id':tid,'fingerprint':k._slice_task_input(batch,k._task(tid),remeasure=True)['fingerprint']} for tid in item['task_ids']]
                from hermes_source_units.model_projection import RENDERER
                if item['template_id']==RENDERER:
                    execution,group=service.execution_slice(w,item['slice_id'])
                    if [g['task_id'] for g in group]!=item['task_ids'] or any(g['canonical_fingerprint']!=i['fingerprint'] for g,i in zip(group,inputs,strict=True)):
                        raise ContractError('STALE_INPUT','$','compact slice canonical inputs changed')
                    measured='sha256:'+fingerprint({'amendment_id':execution['amendment_id'],'tasks':group,
                        'config':execution['config'],'renderer':RENDERER,'template_hash':item['template_hash']})
                    if measured!=item['input_fingerprint']:raise ContractError('STALE_INPUT','$','compact execution fingerprint changed')
                elif not item.get('reslice_count'):
                    creation_config(service,w,item,inputs,batch['slice_config'])
                else:check['note']='resliced task authority measured; creation compatibility is enforced by native worker'
                check['reopen_identity']='valid'
            except (ContractError,OSError,ValueError,KeyError) as exc:
                check.update(reopen_identity='held',reason=str(exc))
            slice_checks.append(check)
        from ingest_kanban import desired_graph
        try:nodes={n.name:n for n in desired_graph(service,w)}
        except (ContractError,OSError,ValueError,KeyError) as exc:issue('dispatch_graph',exc);nodes={}
        for resource,source in sources.items():
            owned=[indexed[t] for t in source['tasks']];active=[r for r in owned if not r.get('excluded')]
            source.update(task_count=len(owned),empty_citations=sum(r.get('latest_kind')=='citation' and r.get('candidate_count')==0 for r in active),
                gap_tasks=[r['task_id'] for r in active if r['gaps']],
                actionable_gap_tasks=[r['task_id'] for r in active if r['gaps'] and r.get('candidate_count',0)>0],
                qa_tasks=[r['task_id'] for r in active if r['qa']])
            name='resource-reduce:'+resource;card=next((c for c in w['kanban']['task_map'] if c['node']==name),None)
            source['binding']={'status':'absent' if not card else 'unchecked'}
            # A revision can temporarily remove unfinished reducers from task_map.
            # Their authenticated prior failures remain part of the finite inventory.
            if not card:
                path=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(name)[:16]}.json')
                if path.is_file():
                    failure=_load_json(path);key=failure.get('idempotency_key')
                    prior=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(key)}.json')
                    pin=next((p for p in w['template_pins'] if p['kind']=='resource-reduce'),None)
                    expected={'workflow_id':wid,'node':name,'task_id':failure.get('task_id'),'actor':w['actor'],
                        'input_fingerprint':failure.get('input_fingerprint'),'template_hash':pin['template_hash'] if pin else None}
                    if (failure.get('node')==name and key==f'ingest:{wid}:resource-reduce:{failure.get("input_fingerprint")}'
                            and prior.is_file() and _load_json(prior)==expected):
                        source['prior_failure']={'ref':path.relative_to(service.vault).as_posix(),
                            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),**failure,
                            'current_input_matches':name in nodes and nodes[name].input_fingerprint==failure.get('input_fingerprint')}
            if card:
                try:
                    node=nodes[name];key=f'ingest:{wid}:{node.kind}:{node.input_fingerprint}'
                    path=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/bindings/{fingerprint(key)}.json')
                    binding=_load_json(path);pin=next(t for t in w['template_pins'] if t['kind']==node.kind)
                    expected={'workflow_id':wid,'node':name,'task_id':card['task_id'],'actor':w['actor'],
                        'input_fingerprint':node.input_fingerprint,'template_hash':pin['template_hash']}
                    if key!=card['idempotency_key'] or binding!=expected:raise ContractError('STALE_INPUT','$','canonical Reduce binding differs')
                    source['binding']={'status':'valid','binding':binding,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                    failure_path=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint(name)[:16]}.json')
                    if failure_path.exists():
                        failure=_load_json(failure_path)
                        if failure.get('task_id')==card['task_id'] and failure.get('idempotency_key')==key:
                            source['current_failure']={'ref':failure_path.relative_to(service.vault).as_posix(),
                                'sha256':hashlib.sha256(failure_path.read_bytes()).hexdigest(),**failure}
                except (ContractError,OSError,ValueError,KeyError,StopIteration) as exc:
                    source['binding']={'status':'blocked','reason':str(exc)}
            if budgets and card and source['binding']['status']=='valid' and not source['actionable_gap_tasks'] and not any(r['errors'] for r in active):
                try:
                    from fixed_reduce import preview_snapshot
                    from fixed_reduce_packets import plan
                    from fixed_semantic import prepare_request_projection
                    packets=plan(adapter,preview_snapshot(adapter,binding),prepare_request_projection)
                    source['budget']={'status':'fits','packets':len(packets),'max_input_codepoints':max(p['input_codepoints'] for p in packets),
                        'sum_input_codepoints':sum(p['input_codepoints'] for p in packets),'correction_reserve':1024,'limit':30000}
                except (ContractError,OSError,ValueError,KeyError) as exc:source['budget']={'status':'blocked','reason':str(exc)}
            elif source['actionable_gap_tasks']:source['budget']={'status':'held_until_evidence_repaired'}
            if source['completed']:source['disposition']='reuse_completed'
            elif any(r['errors'] for r in active) or source['binding']['status']=='blocked' or source['budget']['status']=='blocked':source['disposition']='program_or_input_repair'
            elif source['actionable_gap_tasks']:source['disposition']='targeted_dependency_review'
            elif source['current_failure']:source['disposition']='held_native_failure'
            elif source.get('prior_failure',{}).get('current_input_matches'):source['disposition']='held_prior_native_failure'
            elif source['binding']['status']=='absent':source['disposition']='binding_not_projected'
            else:source['disposition']='mechanically_ready'
        global_input=global_budget(adapter,w) if budgets else {'status':'not_checked','complete_input':False}
        evidence=snapshot(service,w)
        if any(hashlib.sha256(p.read_bytes()).hexdigest()!=sha for p,sha in initial.items()):
            raise ContractError('STALE_INPUT','$','workflow/batch changed during inventory')
    root=Path(__file__).resolve().parent
    report={'contract':'reduce-readiness/v1','workflow_id':wid,'workflow_revision':w['revision'],'batch_id':batch['batch_id'],
        'dependency_contract':CONTRACT,'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'runtime_contract_sha256':hashlib.sha256(b''.join((root/n).read_bytes() for n in ('fixed_reduce.py','fixed_evidence.py','fixed_reduce_packets.py','fixed_semantic.py'))).hexdigest(),
        'control_hashes':{p.relative_to(service.vault).as_posix():sha for p,sha in initial.items()},'evidence':evidence,
        'native_model_calls':0,'resumed':False,'drain':drain,'tasks':rows,'slices':slice_checks,'sources':list(sources.values()),'issues':issues,
        'global_budget':global_input,
        'counts':{'tasks':len(rows),'citations':sum(r.get('latest_kind')=='citation' for r in rows),
            'excluded_tasks':sum(bool(r.get('excluded')) for r in rows),
            'excluded_incomplete_history':sum(bool(r.get('excluded_history_limitation')) for r in rows),
            'active_citations':sum(r.get('latest_kind')=='citation' and not r.get('excluded') for r in rows),
            'empty_citations':sum(r.get('latest_kind')=='citation' and r.get('candidate_count')==0 for r in rows),
            'gap_tasks':sum(bool(r['gaps']) for r in rows),'missing_inspections':sum(len(r['gaps']) for r in rows),
            'actionable_gap_tasks':sum(bool(r['gaps']) and r.get('candidate_count',0)>0 and not r.get('excluded') for r in rows),
            'actionable_missing_inspections':sum(len(r['gaps']) for r in rows if r.get('candidate_count',0)>0 and not r.get('excluded')),
            'qa_tasks':sum(bool(r['qa']) for r in rows),'task_errors':sum(bool(r['errors']) for r in rows)},
        'limitations':'Mechanical readiness is not semantic acceptance. QA is inventoried, not automatically rejected. Empty-result inspection gaps remain visible but are not downstream candidate evidence. Native semantic and network failures require separate disposition; no automatic retries or skips.'}
    report['report_id']=fingerprint(report)
    return report
