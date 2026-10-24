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
           'hermes_source_units/reduce_coverage.py','hermes_source_units/knowledge_build.py',
           'hermes_source_units/schemas/contracts.json']
    return fingerprint({n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in names})


def current(adapter,binding):
    if binding['node']!='global-reduce':return None
    return active(adapter.workflow.vault,adapter.workflow.status(binding['workflow_id']))


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
    import fixed_reduce
    from reduce_disposition import planning_state
    from fixed_semantic_recovery import permitted_tasks
    if node.name!='global-reduce' or active(adapter.workflow.vault,workflow):fail('admission requires an unscoped global Reduce')
    vault=adapter.workflow.vault
    manifest=pinned(vault,entry['manifest_ref'],entry['manifest_sha256'])
    approval=pinned(vault,entry['approval_ref'],entry['approval_sha256'])
    prefix=f"_system/ledgers/ingest-workflows/{workflow['workflow_id']}/coverage-scopes/"
    if any(not entry[k].startswith(prefix) for k in ('manifest_ref','approval_ref')):fail('scope evidence must belong to this workflow')
    if (manifest['workflow_id']!=workflow['workflow_id'] or approval.get('workflow_id')!=workflow['workflow_id']
            or approval.get('actor')!=workflow['actor'] or not approval.get('accept_necessary_coverage_gaps')
            or approval.get('manifest_sha256')!=entry['manifest_sha256']
            or approval.get('stop_after')!='checkpoint_1' or approval.get('new_model_calls_granted')!=0):fail('exact incomplete-scope approval required')
    value=pinned(vault,manifest['source_snapshot_ref'],manifest['source_snapshot_sha256'])
    if value!=fixed_reduce.preview_snapshot(adapter,binding):fail('source snapshot is stale')
    work=pinned(vault,manifest['native_page_work_ref'],manifest['native_page_work_sha256'])
    if work['binding']!=binding or work.get('planning_failure') or work.get('formal_global_submitted'):fail('incomplete or changed native planning')
    permitted=permitted_tasks(adapter,binding)
    permitted[binding['task_id']]={'ended_run_ids':[str(r['id']) for r in runs]}
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
    written=verify_pages(adapter,binding,value,manifest,prove)
    rows=[]
    for p in allowed:
        rows.append({'path':p['storage_destination'],'identity':p['identity'],
            'candidate_refs':[r for c in p['candidate_refs'] for r in value['backend']['mapping'][c]],
            'submitted_content_sha256':p['submitted_content_sha256'],'authored_content_sha256':p['authored_content_sha256']})
    proof={k:entry[k] for k in ('manifest_ref','manifest_sha256','approval_ref','approval_sha256')}
    proof.update(pages=rows,omitted_candidate_refs=[r for p in gaps for c in p['candidate_refs'] for r in value['backend']['mapping'][c]],
        new_model_calls_granted=0,native_bodies_authenticated=len(written),native_planning_calls_authenticated=len(calls),
        gaps_completed=False,source_snapshot_sha256=manifest['source_snapshot_sha256'])
    return {'coverage_admission':proof,'original_calls_preserved':True}


def execute(adapter,binding,value,state,scope):
    """A scoped run has no semantic fallback; missing proof fails closed."""
    from fixed_semantic_recovery import authenticated_replay
    import fixed_global_packets as g
    manifest=scope['manifest']
    # Bind the full source view/backend independently of the new native card.
    source=pinned(adapter.workflow.vault,manifest['source_snapshot_ref'],manifest['source_snapshot_sha256'])
    if any(source[k]!=value[k] for k in ('contract','view','backend')):fail('scoped source inputs changed')
    with authenticated_replay(adapter,binding) as prove:
        written=verify_pages(adapter,binding,value,manifest,prove)
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
