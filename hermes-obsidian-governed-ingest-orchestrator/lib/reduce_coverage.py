"""Zero-call native restoration and exact, approved limited-scope admission."""
import copy
import hashlib
from collections import Counter
from pathlib import Path
from hermes_source_units.source_units import _load_json,_vault_path
from hermes_source_units.validation import fingerprint
from hermes_source_units.reduce_coverage import active,pinned,fail


def contract_hash():
    root=Path(__file__).parent
    names=['reduce_coverage.py','reduce_coverage_reopen.py','fixed_page_work.py','fixed_reduce.py',
           'reduce_execution_recovery.py','hermes_source_units/execution_plan.py',
           'hermes_source_units/reduce_coverage.py','hermes_source_units/knowledge_build.py',
           'hermes_source_units/schemas/contracts.json']
    return fingerprint({n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in names})


def current(adapter,binding):
    if binding['node']!='global-reduce':return None
    return active(adapter.workflow.vault,adapter.workflow.status(binding['workflow_id']))


def journal_seals(adapter,manifest):
    """Seal the finite outcomes already authenticated by the admission."""
    root=adapter.workflow.vault/f"_system/ledgers/ingest-workflows/{manifest['workflow_id']}/output-attempts"
    by_input={}
    for p in root.glob('*.json'):
        record=_load_json(p)
        if record.get('phase')=='page-write':by_input.setdefault(record.get('input_sha256'),[]).append((p,record))
    seals=[]
    for row in manifest['allowed_pages']:
        found=by_input.get(row['input_sha256'],[])
        if len(found)!=1:fail('native body lacks a unique finite journal')
        p,journal=found[0];last=journal['attempts'][-1]
        if last.get('state')!='valid' or last.get('audit_ref')!=row['audit_ref']:fail('accepted native outcome changed')
        audit=_load_json(_vault_path(adapter.workflow.vault,row['audit_ref']))
        seals.append({'ref':p.relative_to(adapter.workflow.vault).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
            'audit_ref':row['audit_ref'],'audit_semantic_sha256':fingerprint({k:v for k,v in audit.items() if k not in ('receipt_refs','validated')})})
    return seals


def reuse_pages(adapter,value,scope):
    """Reuse an admitted native proof only after current source and byte checks."""
    from fixed_destinations import routes,compile_content
    manifest=scope['manifest'];vault=adapter.workflow.vault
    if scope['proof'].get('native_bodies_authenticated')!=len(manifest['allowed_pages']):fail('incomplete admitted native proof')
    for seal in scope['proof'].get('extra_control_seals',[]):
        pinned(vault,seal['ref'],seal['sha256'])
    source=pinned(vault,manifest['source_snapshot_ref'],manifest['source_snapshot_sha256'])
    if any(source[k]!=value[k] for k in ('contract','view','backend')):fail('admitted source authority changed')
    for seal in scope['proof'].get('finite_journal_seals',[]):
        pinned(vault,seal['ref'],seal['sha256'])
        audit=_load_json(_vault_path(vault,seal['audit_ref']))
        if fingerprint({k:v for k,v in audit.items() if k not in ('receipt_refs','validated')})!=seal['audit_semantic_sha256']:
            fail('admitted native audit changed')
    destinations=routes(value,manifest['allowed_pages']+manifest['gaps']);written=[]
    for row in manifest['allowed_pages']:
        raw=_vault_path(vault,row['draft_ref']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['draft_sha256']:fail('admitted native body changed')
        page={k:copy.deepcopy(row[k]) for k in ('identity','path','candidate_refs','draft_ref','audit_ref','input_sha256')}
        page['content']=raw.decode('utf-8');compiled,_=compile_content(value,page,destinations)
        normalized=compiled.replace('\r\n','\n').replace('\r','\n').rstrip()+'\n'
        if (destinations[row['path']]!=row['storage_destination']
                or hashlib.sha256(compiled.encode()).hexdigest()!=row['submitted_content_sha256']
                or hashlib.sha256(normalized.encode()).hexdigest()!=row['authored_content_sha256']):fail('admitted output differs')
        written.append(page)
    return written


def verify_pages(adapter,binding,value,manifest,prove):
    """Authenticate exact finite native output, never a cached 'valid' label."""
    from fixed_page_work import measure
    from fixed_semantic import HermesCaller,prepare_request_projection
    from fixed_semantic_media import image_catalog,select_images
    from fixed_page_replay import read_outcome
    from fixed_destinations import routes,compile_content
    vault=adapter.workflow.vault;written=[]
    destinations=routes(value,manifest['allowed_pages']+manifest['gaps'])
    from urllib.parse import unquote,urlsplit
    import posixpath,re
    gaps={destinations[p['path']].casefold() for p in manifest['gaps']}
    gap_names={p['identity']['canonical_name'] for p in manifest['gaps']}
    gap_names.update(a for p in manifest['gaps'] for a in p['identity'].get('aliases',[]))
    gap_names={n.casefold() for n in gap_names}
    for p in manifest['gaps']:
        for path in (p['path'],destinations[p['path']]):
            gap_names.update((path.casefold(),path.removesuffix('.md').casefold(),
                posixpath.basename(path).removesuffix('.md').casefold()))
    for row in manifest['allowed_pages']:
        page={k:copy.deepcopy(row[k]) for k in ('identity','path','candidate_refs')}
        view,size=measure(adapter,value,page,HermesCaller(),prepare_request_projection)
        if size>30000 or fingerprint(view)!=row['input_sha256']:fail('page authority changed')
        images=select_images(view['original_evidence']['tasks'],image_catalog(adapter,view['original_evidence'])) if 'original_evidence' in view else []
        result=read_outcome(adapter,binding,view,prove,images)
        if not result or result['kind']!='valid' or result['ref']!=row['audit_ref']:fail('missing trusted native body')
        content=result['draft']['content'];raw=content.encode('utf-8')
        expected=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/page-drafts/{fingerprint(view)}.md"
        if row['draft_ref']!=expected or _vault_path(vault,expected).read_bytes()!=raw or hashlib.sha256(raw).hexdigest()!=row['draft_sha256']:
            fail('native body bytes changed')
        page['content']=content
        compiled,edits=compile_content(value,page,destinations)
        normalized=compiled.replace('\r\n','\n').replace('\r','\n').rstrip()+'\n'
        if (destinations[row['path']]!=row['storage_destination']
                or hashlib.sha256(compiled.encode()).hexdigest()!=row['submitted_content_sha256']
                or hashlib.sha256(normalized.encode()).hexdigest()!=row['authored_content_sha256']):fail('approved destination/content differs')
        # Explicit authored links cannot introduce a dependency on a held page.
        # This checks actual Markdown/wiki edges, not broad source proximity.
        for target in re.findall(r'!?\[\[([^\]]+)\]\]',compiled):
            target=unquote(target.split('|')[0].split('#')[0]).casefold()
            if target in gap_names or target in gaps or target+'.md' in gaps:fail('allowed page links to a held page')
        for href in re.findall(r'!?\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)',compiled):
            parsed=urlsplit(href.strip('<>'))
            if not parsed.scheme and parsed.path:
                target=posixpath.normpath(posixpath.join(posixpath.dirname(destinations[row['path']]),unquote(parsed.path)))
                if target.casefold() in gaps:fail('allowed page depends on deferred content')
        written.append({**page,'draft_ref':expected,'audit_ref':row['audit_ref'],'input_sha256':row['input_sha256']})
    return written


def verify(adapter,workflow,node,binding,entry,failure,runs):
    if entry.get('amendment_id'):
        from citation_amendment_admission import verify as local_verify
        return local_verify(adapter,workflow,node,binding,entry)
    import fixed_reduce
    from reduce_disposition import planning_state
    from fixed_semantic_recovery import permitted_tasks
    previous=active(adapter.workflow.vault,workflow)
    if node.name!='global-reduce':fail('admission requires global Reduce')
    if previous:
        if entry.get('supersedes_scope')!=previous['ref'] or entry.get('manifest_sha256')!=previous['ref']['manifest_sha256']:
            fail('recertification must preserve the exact approved page/gap scope')
        batch=adapter.workflow.knowledge._batch(workflow['batch_id'])
        if batch['run_ids'] or batch.get('global_reduction_id'):fail('withdraw consumed scope before recertifying')
        basis=entry.get('basis',[])
        if not basis:fail('recertification requires a pinned correction cause')
        for item in basis:pinned(adapter.workflow.vault,item['ref'],item['sha256'])
    vault=adapter.workflow.vault
    manifest=pinned(vault,entry['manifest_ref'],entry['manifest_sha256'])
    approval=pinned(vault,entry['approval_ref'],entry['approval_sha256'])
    prefix=f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}/coverage-scopes/"
    if any(not entry[k].startswith(prefix) for k in ('manifest_ref','approval_ref')):fail('scope evidence must belong to this workflow')
    if (manifest['workflow_id']!=workflow['workflow_id'] or approval.get('workflow_id')!=workflow['workflow_id']
            or approval.get('actor')!=workflow['actor'] or not approval.get('accept_necessary_coverage_gaps')
            or approval.get('manifest_sha256')!=entry['manifest_sha256']
            or approval.get('stop_after')!='checkpoint_1' or approval.get('new_model_calls_granted')!=0):fail('exact incomplete-scope approval required')
    source=pinned(vault,manifest['source_snapshot_ref'],manifest['source_snapshot_sha256'])
    value=fixed_reduce.preview_snapshot(adapter,binding)
    if any(source[k]!=value[k] for k in ('contract','view','backend')):fail('source snapshot is stale')
    work=pinned(vault,manifest['native_page_work_ref'],manifest['native_page_work_sha256'])
    if work['binding']!=source['binding'] or work.get('planning_failure') or work.get('formal_global_submitted'):fail('incomplete or changed native planning')
    permitted=permitted_tasks(adapter,binding)
    permitted[binding['task_id']]={'ended_run_ids':[str(r['id']) for r in runs]}
    if previous:
        # Existing admitted planning remains bound to the identical complete source
        # snapshot; a new card does not require another semantic/native replay.
        state={'pages':manifest['allowed_pages']+manifest['gaps'],'omitted_candidate_refs':[]}
        calls=[None]*previous['proof']['native_planning_calls_authenticated'];prove=None
    else:
        state,remaining,pending,calls,prove=planning_state(adapter,binding,value,permitted)
        if remaining or pending:fail('every proposal needs completed native ownership')
    allowed=manifest['allowed_pages'];gaps=manifest['gaps'];allrows=allowed+gaps
    paths=[p['path'] for p in allrows]
    if len(set(paths))!=len(paths) or len({fingerprint(p['identity']) for p in allrows})!=len(paths):fail('scope identities or paths overlap')
    actual=Counter(c for p in allrows for c in p['candidate_refs'])
    planned=Counter(c for p in state['pages'] for c in p['candidate_refs'])
    if actual!=planned or any(n!=1 for n in actual.values()) or state['omitted_candidate_refs']:fail('candidate ownership or complete accounting differs')
    if (len(allowed)!=manifest['allowed_page_count'] or len(gaps)!=manifest['held_page_count']
            or sum(len(p['candidate_refs']) for p in gaps)!=manifest['deferred_candidate_occurrences']
            or manifest.get('whole_batch_coverage_complete') is not False):fail('incomplete scope counts differ')
    for a,b in ((allowed,work['written']),(gaps,work['held'])):
        if {p['path']:p['candidate_refs'] for p in a}!={p['path']:p['candidate_refs'] for p in b}:fail('approved set differs from current durable outcomes')
    written=reuse_pages(adapter,value,previous) if previous else verify_pages(adapter,binding,value,manifest,prove)
    rows=[]
    for p in allowed:
        rows.append({'path':p['storage_destination'],'identity':p['identity'],
            'candidate_refs':[r for c in p['candidate_refs'] for r in value['backend']['mapping'][c]],
            'submitted_content_sha256':p['submitted_content_sha256'],'authored_content_sha256':p['authored_content_sha256']})
    proof={k:entry[k] for k in ('manifest_ref','manifest_sha256','approval_ref','approval_sha256')}
    proof.update(pages=rows,omitted_candidate_refs=[r for p in gaps for c in p['candidate_refs'] for r in value['backend']['mapping'][c]],
        new_model_calls_granted=0,native_bodies_authenticated=len(written),native_planning_calls_authenticated=len(calls),
        gaps_completed=False,source_snapshot_sha256=manifest['source_snapshot_sha256'])
    from hermes_source_units.reduce_coverage import task_partition
    proof.update(task_partition(adapter.workflow.knowledge,value['backend']['tasks'],rows))
    proof['finite_journal_seals']=journal_seals(adapter,manifest)
    result={'coverage_admission':proof,'original_calls_preserved':True}
    if previous:
        proof['reused_native_authorization']=previous['ref']
        result.update(supersedes_scope=previous['ref'],basis=entry['basis'])
    return result


def verify_validator(adapter,workflow,node,binding,entry):
    """Retry only a deterministic validator; preserve every native domain result."""
    if node.name!='checkpoint-1-validate':fail('validation recovery owns checkpoint_1 validation only')
    knowledge=adapter.workflow.knowledge;batch=knowledge._batch(workflow['batch_id'])
    if not batch.get('global_reduction_id') or not batch['run_ids']:fail('reuse a durable global result before validation retry')
    basis=entry.get('basis',[])
    if not basis or len(basis)>8:fail('validator retry needs a pinned correction cause')
    for item in basis:pinned(adapter.workflow.vault,item['ref'],item['sha256'])
    checks=[]
    for rid in batch['run_ids']:
        path=knowledge._run_path(rid);knowledge.validate_run(rid)
        checks.append({'run_id':rid,'manifest_ref':path.relative_to(adapter.workflow.vault).as_posix(),
                       'manifest_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    from hermes_source_units.reduce_coverage import batch_outcome
    outcome=batch_outcome(knowledge,batch)
    path=knowledge._global_reduction_path(batch['batch_id'],batch['global_reduction_id'])
    return {'validator_recovery':{'global_reduction_id':batch['global_reduction_id'],
        'global_ref':path.relative_to(adapter.workflow.vault).as_posix(),
        'global_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'draft_runs':checks,
        'basis':basis,'new_model_calls_granted':0,'domain_outputs_preserved':True,
        'coverage_complete':outcome.get('coverage_complete'),'pause_released':False}}


def execute(adapter,binding,value,state,scope):
    """A scoped run has no semantic fallback; missing proof fails closed."""
    from fixed_semantic_recovery import authenticated_replay
    import fixed_global_packets as g
    manifest=scope['manifest']
    # Bind the full source view/backend independently of the new native card.
    source=pinned(adapter.workflow.vault,manifest['source_snapshot_ref'],manifest['source_snapshot_sha256'])
    if any(source[k]!=value[k] for k in ('contract','view','backend')):fail('scoped source inputs changed')
    if not scope['proof'].get('finite_journal_seals'):fail('scope lacks sealed finite native outcomes')
    written=reuse_pages(adapter,value,scope)
    effective=copy.deepcopy(state)
    effective['pages']=[{k:p[k] for k in ('identity','path','candidate_refs')} for p in manifest['allowed_pages']]
    effective['omitted_candidate_refs']=[c for p in manifest['gaps'] for c in p['candidate_refs']]
    effective['reasons'].append('Explicit limited-scope acceptance; deferred necessary coverage remains incomplete.')
    draft=g.complete(value,effective);bodies={p['path']:p['content'] for p in written}
    for p in draft['pages']:p['content']=bodies[p['path']]
    from hermes_source_units.source_units import _write_atomic,_json_bytes
    ref=f"_system/ledgers/ingest-workflows/{binding['workflow_id']}/semantic-inputs/page-work-{value['input_id']}.json"
    _write_atomic(_vault_path(adapter.workflow.vault,ref),_json_bytes({'contract':'native-page-work/v1','binding':binding,
        'original_plan_sha256':fingerprint(state),'current_plan_sha256':fingerprint(effective),
        'written':[{k:v for k,v in p.items() if k!='content'} for p in written],'held':manifest['gaps'],
        'planning_failure':None,'successful_call_refs':[p['audit_ref'] for p in written],
        'formal_global_submitted':False,'coverage_scope':scope['ref'],'gaps_completed':False}))
    return draft,[p['audit_ref'] for p in written]
