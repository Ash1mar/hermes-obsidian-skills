"""New exact limited admission after a locally revised native result."""
import copy
import hashlib
from pathlib import Path
from hermes_source_units.citation_amendment import state,plan,root,save,fail
from hermes_source_units.source_units import _load_json,_vault_path,_json_bytes,_write_atomic
from hermes_source_units.validation import fingerprint

def manifest(adapter,value,work,frozen,amendment):
    from fixed_destinations import routes,compile_content
    import fixed_reduce
    vault=adapter.workflow.vault;allrows=work['written']+work['held'];destinations=routes(value,allrows)
    written=[]
    for row in work['written']:
        raw=_vault_path(vault,row['draft_ref']).read_bytes();page={**row,'content':raw.decode()}
        compiled,_=compile_content(value,page,destinations)
        normalized=compiled.replace('\r\n','\n').replace('\r','\n').rstrip()+'\n'
        written.append({**row,'draft_sha256':hashlib.sha256(raw).hexdigest(),
            'storage_destination':destinations[row['path']],
            'submitted_content_sha256':hashlib.sha256(compiled.encode()).hexdigest(),
            'authored_content_sha256':hashlib.sha256(normalized.encode()).hexdigest()})
    source=_vault_path(vault,work['input_ref'])
    result={'workflow_id':value['binding']['workflow_id'],'allowed_pages':written,'gaps':work['held'],
            'allowed_page_count':len(written),'held_page_count':len(work['held']),
            'deferred_candidate_occurrences':sum(len(p['candidate_refs']) for p in work['held']),
            'whole_batch_coverage_complete':False,
            'source_snapshot_ref':work['input_ref'],'source_snapshot_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'amendment_id':amendment['operation_id'],'prior_manifest_sha256':frozen['scope']['ref']['manifest_sha256']}
    path=root(vault,value['binding']['workflow_id'])/amendment['operation_id']/('manifest-'+fingerprint(result)+'.json')
    raw=_json_bytes(result);_write_atomic(path,raw)
    return path.relative_to(vault).as_posix(),hashlib.sha256(raw).hexdigest()

def verify(adapter,workflow,node,binding,entry):
    from reduce_coverage import pinned
    import fixed_reduce
    from citation_amendment_calls import authenticate
    from citation_amendment_worker import global_plan
    from citation_local_proof import page_proof
    value=state(adapter.workflow.vault,workflow['workflow_id']);frozen=plan(adapter.workflow.vault,value)
    if value['phase']!='awaiting_acceptance' or entry.get('amendment_id')!=value['operation_id']:
        fail('local admission requires the completed local amendment')
    selected=pinned(adapter.workflow.vault,entry['manifest_ref'],entry['manifest_sha256'])
    if entry['manifest_ref']!=value['manifest_ref'] or entry['manifest_sha256']!=value['manifest_sha256']:
        fail('approve the exact newly generated manifest; old scope hashes cannot be reused')
    approval=pinned(adapter.workflow.vault,entry['approval_ref'],entry['approval_sha256'])
    if (approval.get('manifest_sha256')!=entry['manifest_sha256'] or approval.get('actor')!=workflow['actor']
            or approval.get('workflow_id')!=workflow['workflow_id'] or approval.get('accept_necessary_coverage_gaps') is not True
            or approval.get('stop_after')!='checkpoint_1' or approval.get('new_model_calls_granted')!=0):
        fail('new exact incomplete-scope approval is required')
    source=pinned(adapter.workflow.vault,selected['source_snapshot_ref'],selected['source_snapshot_sha256'])
    current=fixed_reduce.preview_snapshot(adapter,binding)
    if any(source[k]!=current[k] for k in ('contract','view','backend')):fail('amended source authority changed')
    work=pinned(adapter.workflow.vault,value['work_ref'],value['work_sha256'])
    # All fresh domain products are authenticated against actual native requests.
    reuse_paths={r['path'] for r in work['reuse_proofs']}
    required_bodies={p['audit_ref'] for p in selected['allowed_pages'] if p['path'] not in reuse_paths}
    authenticated={phase:authenticate(adapter,workflow['workflow_id'],phase,required_refs=required_bodies if phase=='body' else None)
                   for phase in ('citation','source','plan','body')}
    if selected['gaps']!=work['held']:fail('new manifest must preserve actual held status and failure evidence')
    from citation_amendment_control import accept_successor
    # The stored successor was checked before acceptance; recheck both its raw
    # immutable bytes and the actual native citation body at final admission.
    from citation_amendment_calls import verify_citation_body
    verify_citation_body(adapter,frozen,value['successor'],authenticated['citation'][0])
    from hermes_source_units.citation_revision_impact import successor_delta
    successor_delta(_load_json(_vault_path(adapter.workflow.vault,frozen['old_pass_ref'])),value['successor'],frozen['selection']['candidate_ids'])
    # Reconstruct planning from real replayed native responses; a caller that
    # requests a fresh response here is rejected by the phase guard.
    class ReplayOnly:pass
    planned,mapping,_,_=global_plan(adapter,binding,ReplayOnly(),current,frozen,value)
    if planned!=work['state']:fail('saved local plan differs from reconstructed native ownership')
    by_path={p['path']:p for p in planned['pages']}
    if set(by_path)!={p['path'] for p in selected['allowed_pages']+selected['gaps']}:fail('new manifest page ownership differs')
    all_members=[c for p in selected['allowed_pages']+selected['gaps'] for c in p['candidate_refs']]
    if len(all_members)!=len(set(all_members)) or set(all_members)!=set(current['backend']['mapping']):
        fail('global unique candidate ownership/coverage is incomplete')
    reuse={r['path']:r for r in work['reuse_proofs']}
    bodies={c['audit_ref']:c for c in authenticated['body']}
    from fixed_destinations import routes,compile_content
    destinations=routes(current,selected['allowed_pages']+selected['gaps']);rows=[]
    from urllib.parse import unquote,urlsplit
    import re,posixpath,json
    from fixed_semantic_recovery import _request_text
    gap_paths={destinations[p['path']].casefold() for p in selected['gaps']}
    gap_names={n.casefold() for p in selected['gaps'] for n in [p['path'],p['identity']['canonical_name'],*p['identity'].get('aliases',[]),posixpath.basename(p['path']).removesuffix('.md')]}
    for row in selected['allowed_pages']:
        p=by_path[row['path']]
        if any(p[k]!=row[k] for k in ('identity','path','candidate_refs')):fail('manifest changed the actual native grouping')
        raw=_vault_path(adapter.workflow.vault,row['draft_ref']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['draft_sha256']:fail('native draft bytes changed')
        if row['path'] in reuse:
            old=reuse[row['path']]['origin']
            original=next((p for p in frozen['scope']['manifest']['allowed_pages'] if p['path']==row['path']),None)
            if old!=original:fail('reuse origin is not the exact previously admitted native page')
            expected=page_proof(frozen['old_source'],current,old,p,adapter)
            if expected!=reuse[row['path']]['proof'] or row['audit_ref']!=old['audit_ref'] or row['draft_sha256']!=old['draft_sha256']:
                fail('local reuse proof or native origin changed')
        else:
            call=bodies.get(row['audit_ref'])
            if not call or call['body'].get('content','').encode()!=raw:fail('new draft lacks actual native authorship')
            observed=json.loads(_request_text(call['wire']['user']))['input']
            guidance=work.get('guidance_authority',{}).get(row['path'])
            if row.get('review_guidance')!=guidance:fail('body guidance differs from the explicit recovery authority')
            if observed!={**fixed_reduce.page_view(adapter,current,p),'page_scope_contract':'assigned-candidates/v1',
                           **({'amendment_review_guidance':guidance} if guidance else {})}:
                fail('native writer did not observe the current complete page input')
        compiled,_=compile_content(current,{**p,'content':raw.decode()},destinations)
        for target in re.findall(r'!?\[\[([^\]]+)\]\]',compiled):
            if unquote(target.split('|')[0].split('#')[0]).casefold() in gap_names:fail('allowed page depends on a held page')
        for href in re.findall(r'!?\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)',compiled):
            uri=urlsplit(href.strip('<>'))
            if not uri.scheme and uri.path and posixpath.normpath(posixpath.join(posixpath.dirname(destinations[p['path']]),unquote(uri.path))).casefold() in gap_paths:
                fail('allowed page links to deferred content')
        normalized=compiled.replace('\r\n','\n').replace('\r','\n').rstrip()+'\n'
        if (hashlib.sha256(compiled.encode()).hexdigest()!=row['submitted_content_sha256']
                or hashlib.sha256(normalized.encode()).hexdigest()!=row['authored_content_sha256']
                or destinations[row['path']]!=row['storage_destination']):fail('current output compilation differs')
        rows.append({'path':row['storage_destination'],'identity':p['identity'],
                     'candidate_refs':[r for h in p['candidate_refs'] for r in current['backend']['mapping'][h]],
                     'submitted_content_sha256':row['submitted_content_sha256'],'authored_content_sha256':row['authored_content_sha256']})
    # Recheck old native seals. They certify original authorship, while the new
    # per-page proofs above establish compatibility under changed upstream IDs.
    seals=frozen['scope']['proof']['finite_journal_seals']
    for seal in seals:
        pinned(adapter.workflow.vault,seal['ref'],seal['sha256'])
        audit=_load_json(_vault_path(adapter.workflow.vault,seal['audit_ref']))
        if fingerprint({k:v for k,v in audit.items() if k not in ('receipt_refs','validated')})!=seal['audit_semantic_sha256']:fail('old native audit changed')
    for calls in authenticated.values():
        for call in calls:
            audit=_load_json(_vault_path(adapter.workflow.vault,call['audit_ref']))
            seals=[*seals,{'ref':value['plan_ref'],'sha256':value['plan_sha256'],'audit_ref':call['audit_ref'],
                          'audit_semantic_sha256':fingerprint({k:v for k,v in audit.items() if k not in ('receipt_refs','validated')})}]
    from hermes_source_units.reduce_coverage import task_partition
    proof={k:entry[k] for k in ('manifest_ref','manifest_sha256','approval_ref','approval_sha256')}
    proof.update(pages=rows,omitted_candidate_refs=[r for p in selected['gaps'] for h in p['candidate_refs'] for r in current['backend']['mapping'][h]],
        new_model_calls_granted=0,native_bodies_authenticated=len(rows),
        native_planning_calls_authenticated=frozen['scope']['proof']['native_planning_calls_authenticated']+len(authenticated['plan']),
        finite_journal_seals=seals,gaps_completed=False,source_snapshot_sha256=selected['source_snapshot_sha256'],
        local_amendment_proofs_ref=value['work_ref'])
    proof['extra_control_seals']=[{'ref':value['work_ref'],'sha256':value['work_sha256']},
                                  {'ref':value['plan_ref'],'sha256':value['plan_sha256']}]
    for ref in value.get('source_intents',{}).values():
        proof['extra_control_seals'].append({'ref':ref,'sha256':hashlib.sha256(_vault_path(adapter.workflow.vault,ref).read_bytes()).hexdigest()})
    proof['local_revision']={'operation_id':value['operation_id'],'predecessor_pass_id':frozen['selection']['pass_id'],
                             'successor_pass_id':value['successor']['pass_id'],'delta':value['delta'],
                             'source_intents':value.get('source_intents',{}),'no_global_retry_budget_reset':True}
    proof.update(task_partition(adapter.workflow.knowledge,current['backend']['tasks'],rows))
    return {'coverage_admission':proof,'original_calls_preserved':True}

def admit(adapter,request):
    from citation_amendment_control import drained,mutation,stored_step
    from reduce_coverage import contract_hash
    from reduce_execution_recovery import recover_reduce_execution
    service=adapter.workflow;wid=request['workflow_id'];value=state(service.vault,wid);w=service.status(wid);drained(adapter,w)
    if value['phase']!='awaiting_acceptance':fail('finish local native work before admission')
    approval_ref=request.get('approval_ref');approval_sha=request.get('approval_sha256')
    card=next(c for c in w['kanban']['task_map'] if c['node']=='global-reduce')
    failure=_vault_path(service.vault,f'_system/ledgers/ingest-workflows/{wid}/reports/failed-{fingerprint("global-reduce")[:16]}.json')
    entry={'node':'global-reduce','task_id':card['task_id'],'failure_sha256':hashlib.sha256(failure.read_bytes()).hexdigest(),
           'amendment_id':value['operation_id'],'manifest_ref':value['manifest_ref'],'manifest_sha256':value['manifest_sha256'],
           'approval_ref':approval_ref,'approval_sha256':approval_sha}
    generation=value.get('generation',0)
    recovery=stored_step(service,wid,value,'new_scope_request:'+str(generation),lambda:mutation(w,
        recovery_id='local-admit-'+fingerprint({'operation':value['operation_id'],'generation':generation})[:40],
        recovery_scope='reduce-coverage/v1',target_contract_sha256=contract_hash(),reason='Reaccept the exact locally revised subset; retain all necessary gaps.',entries=[entry]))
    result=recover_reduce_execution(adapter,recovery)
    return {**result,'resumed':False,'phase':'awaiting_acceptance'}
