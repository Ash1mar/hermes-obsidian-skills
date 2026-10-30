"""Append-only partial Build commits, with original runs and source gaps intact.

An intent precedes canonical writes. Its receipt follows complete verification.
An interrupted intent must be finished before another selection can be submitted.
The original Reduce manifest/drafts are never split, renamed or overwritten.
"""
import copy
import hashlib
from .source_units import _load_json, _vault_path, _write_atomic, _json_bytes
from .validation import ContractError, fingerprint, validate_record

CONTRACT = 'partial-build-finalize/v1'


def fail(message, code='FINALIZE_DISPOSITION_INVALID'):
    raise ContractError(code, '$', message)


def pin(vault, ref):
    return {'ref': ref, 'sha256': hashlib.sha256(_vault_path(vault, ref).read_bytes()).hexdigest()}


def pinned(vault, pointer):
    raw = _vault_path(vault, pointer['ref']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != pointer['sha256']:
        fail('pinned Finalize evidence changed', 'SOURCE_CHANGED')
    import json
    return json.loads(raw)


def root(service, run):
    return service._run_path(run['run_id']).parent/'partial-finalize'


def authority(service, actor):
    from .workflow_guard import _ACTIVE, _BINDING
    active, binding = _ACTIVE.get(), _BINDING.get()
    if (not active or not binding or active['kind'] != 'build-finalize'
            or binding['node'] != 'build-finalize' or active['actor'] != actor
            or active['vault'] != str(service.vault)
            or active['workflow_id'] != binding['workflow_id']):
        fail('partial Finalize requires the live bound native Build writer', 'ACCESS_DENIED')
    return binding

def enabled(service,binding,run):
    from .finalize_revision_state import load,work_id
    permission=load(service.vault,binding['workflow_id'])['works'].get(work_id(run['run_id'],'*'))
    return bool(permission and permission['state']=='enabled' and permission['run_sha256']==fingerprint(_load_json(service._run_path(run['run_id']))))

def prepare_storage(service,workflow):
    """Create mechanical output containers before sealing absent Pass parents.

    A sealed parent must keep rejecting new children. Existing unsealed output
    containers allow ordinary writes without weakening that directory protection.
    No page body, revision, source task or acceptance record is created here.
    """
    import os
    if (os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')
            or workflow['state']!='cancelled' or workflow['current_stage']!='build_finalizing'):
        fail('Finalize storage preparation requires the stopped operator','ACCESS_DENIED')
    outputs=[_vault_path(service.vault,'_system/knowledge-builds/.finalize.lock'),
             _vault_path(service.vault,'_system/knowledge-builds/page-revisions')]
    if any(p.is_symlink() for p in outputs):fail('Finalize output container is a symlink','ACCESS_DENIED')
    outputs[0].parent.mkdir(parents=True,exist_ok=True)
    with outputs[0].open('a',encoding='utf-8'):pass
    outputs[1].mkdir(exist_ok=True)
    return [p.relative_to(service.vault).as_posix() for p in outputs]


def chain(service, run):
    original_run=_load_json(service._run_path(run['run_id']))
    rows = {}
    for path in root(service, run).glob('*.intent.json'):
        value = _load_json(path)
        if (path.name != fingerprint(value)+'.intent.json' or value.get('contract') != CONTRACT
                or value.get('run_id') != run['run_id'] or value.get('run_sha256') != fingerprint(original_run)):
            fail('partial Finalize intent or original run changed', 'SOURCE_CHANGED')
        rows[path.relative_to(service.vault).as_posix()] = value
    ordered = []; previous = None
    while rows:
        matches = [(ref, value) for ref, value in rows.items() if value['predecessor'] == previous]
        if len(matches) != 1:
            fail('partial Finalize intent chain is disconnected or forked')
        ref, value = matches[0]; rows.pop(ref)
        pointer = pin(service.vault, ref); ordered.append((pointer, value)); previous = pointer
    return ordered


def receipt_path(service, pointer):
    path = _vault_path(service.vault, pointer['ref'])
    return path.with_name(path.name.replace('.intent.json', '.receipt.json'))


def subject_for(run, page, registry):
    decision = next(d for d in run['decisions'] if d['page_id'] == page['page_id'])
    prior = next((s for s in registry['subjects'] if s['subject_id'] == page['subject_id']), None)
    identity = decision['identity']
    return {'subject_id': page['subject_id'], 'page_id': page['page_id'],
        **{k: identity[k] for k in ('kind', 'identity_key', 'canonical_name', 'aliases')},
        'status': 'active', 'current_path': page['path'], 'current_revision_id': page['revision_id'],
        'created_by_run': prior['created_by_run'] if prior else run['run_id']}


def expected_receipt(pointer, intent):
    return {'contract': CONTRACT, 'intent': pointer, 'run_id': intent['run_id'],
        'committed_page_ids': [p['page_id'] for p in intent['pages']],
        'registry_sha256': fingerprint(intent['registry_after']), 'source_tasks_completed': False}


def registry_reachable(service, before, after):
    if before==after:return True
    current=before;seen=set()
    candidates=[]
    for path in (service.vault/'_system/knowledge-builds').glob('*/partial-finalize/*.intent.json'):
        value=_load_json(path)
        if path.name!=fingerprint(value)+'.intent.json' or value.get('contract')!=CONTRACT:
            fail('partial registry transition evidence changed','SOURCE_CHANGED')
        candidates.append(value)
    while fingerprint(current) not in seen:
        seen.add(fingerprint(current))
        matches=[i for i in candidates if i['registry_before']==current and i['registry_after']!=current]
        if len(matches)!=1:return False
        current=matches[0]['registry_after']
        if current==after:return True
    return False


def verify_outputs(service, intent):
    for page in intent['pages']:
        side = _vault_path(service.vault,
            f"_system/knowledge-builds/page-revisions/{page['page_id']}/{page['revision_id']}.json")
        if _load_json(side) != page or hashlib.sha256(_vault_path(service.vault, page['path']).read_bytes()).hexdigest() != page['authored_sha256']:
            fail('partial committed page or sidecar differs', 'SOURCE_CHANGED')
    if not registry_reachable(service,intent['registry_after'],service._registry()):
        fail('partial registry differs from the exact committed intent', 'SOURCE_CHANGED')


def progress(service, run, *, verify=True):
    entries = chain(service, run); committed = {}; pending = None
    latest_registry = None; original_registry = None; base = None
    for pointer, intent in entries:
        basis = pinned(service.vault, intent['snapshot'])
        if base is None:
            base = basis; original_registry = intent['registry_before']
        if latest_registry is not None and intent['registry_before'] != latest_registry:
            fail('partial registry transitions are not contiguous')
        if not registry_reachable(service,basis['backend']['registry'],intent['registry_before']):
            fail('partial intent does not pin its actual before-registry')
        latest_registry = intent['registry_after']
        rp = receipt_path(service, pointer)
        if rp.exists():
            if _load_json(rp) != expected_receipt(pointer, intent):
                fail('partial Finalize receipt changed', 'SOURCE_CHANGED')
            committed.update({p['page_id']: {'page': p, 'review': r, 'snapshot': intent['snapshot']}
                for p, r in zip(intent['pages'], intent['reviews'])})
        else:
            if pointer != entries[-1][0]:
                fail('an unfinished partial intent cannot have a successor')
            pending = (pointer, intent)
    if verify and entries:
        allowed = [latest_registry]
        if pending: allowed.append(pending[1]['registry_before'])
        if not any(registry_reachable(service,r,service._registry()) for r in allowed):
            fail('live registry differs from all authorized partial states', 'SOURCE_CHANGED')
        # Old canonical revisions remain immutable even after a later page revision.
        for pointer, intent in entries:
            if not receipt_path(service, pointer).exists():continue
            for page in intent['pages']:
                side = _vault_path(service.vault,
                    f"_system/knowledge-builds/page-revisions/{page['page_id']}/{page['revision_id']}.json")
                if _load_json(side) != page:
                    fail('historical partial revision changed', 'SOURCE_CHANGED')
        for item in committed.values():
            page = item['page']
            # Pending writes can already have replaced a prior committed revision.
            if pending and any(p['page_id'] == page['page_id'] for p in pending[1]['pages']):continue
            if hashlib.sha256(_vault_path(service.vault, page['path']).read_bytes()).hexdigest() != page['authored_sha256']:
                fail('partial canonical page changed', 'SOURCE_CHANGED')
    return {'entries': entries, 'committed': committed, 'pending': pending,
        'registry_before': original_registry, 'registry_after': latest_registry, 'basis': base}


def prepare(service, run, request, registry, pages, proposed_registry):
    binding = authority(service, run['actor'])
    from .finalize_revision_state import load,work_id
    original_run=_load_json(service._run_path(run['run_id']))
    permission=load(service.vault,binding['workflow_id'])['works'].get(work_id(run['run_id'],'*'))
    if not permission or permission['state']!='enabled' or permission['run_sha256']!=fingerprint(original_run):
        fail('partial Finalize requires an explicit current operator enablement','ACCESS_DENIED')
    manifest = pinned(service.vault, request['partial_manifest'])
    if (manifest.get('contract') != 'build-evidence-disposition/v1'
            or manifest['binding'] != binding or not manifest.get('evidence_checks', {}).get('ok')):
        fail('partial submission lacks the current bound common-evidence checks')
    snapshot = pinned(service.vault, manifest['snapshot'])
    if (snapshot['binding'] != binding or run not in snapshot['backend']['runs']
            or not registry_reachable(service,snapshot['backend']['registry'],registry)):
        fail('partial submission snapshot or before-registry differs', 'STALE_INPUT')
    selections = {r['page_id']: r for r in request['reviews']}
    partition=[r for r in manifest['items'] if r['logical_work']['run_id']==run['run_id']]
    if len(partition)!=len(run['page_revisions']) or {r['logical_work']['page_id'] for r in partition}!={p['page_id'] for p in run['page_revisions']}:
        fail('partial manifest must retain every original page and gap exactly once')
    if any(r['state']!='ready' for r in partition if r['logical_work']['page_id'] in selections):
        fail('a deferred/unknown page cannot enter the ready selection')
    eligible = {r['page_id']: r for r in manifest['prepared_reviews'] if r['run_id'] == run['run_id']}
    if not selections or any({k:v for k,v in eligible.get(pid, {}).items() if k != 'run_id'} != review for pid,review in selections.items()):
        fail('partial selection is not an exact subset of authenticated ready reviews')
    state = progress(service, run)
    ordered_reviews=[selections[p['page_id']] for p in pages]
    if state['pending']:
        pointer, intent = state['pending']
        if intent['reviews'] != ordered_reviews or intent['pages'] != pages:
            fail('finish the exact interrupted partial intent before selecting new work')
        return pointer, intent
    if state['entries']:
        pointer,intent=state['entries'][-1]
        if intent['reviews']==ordered_reviews and intent['pages']==pages:
            return pointer,intent
    expected = copy.deepcopy(registry)
    for page in pages:
        subject = subject_for(run, page, expected)
        old = next((s for s in expected['subjects'] if s['subject_id'] == page['subject_id']), None)
        if old: old.update(subject)
        else: expected['subjects'].append(subject)
    if expected['subjects'] != registry['subjects']:expected['revision'] += 1
    if expected != proposed_registry:
        fail('partial commit registry change exceeds the selected pages')
    intent = {'contract': CONTRACT, 'run_id': run['run_id'], 'run_sha256': fingerprint(original_run),
        'workflow_id': binding['workflow_id'], 'snapshot': manifest['snapshot'],
        'disposition': request['partial_manifest'], 'predecessor': state['entries'][-1][0] if state['entries'] else None,
        'registry_before': registry, 'registry_after': proposed_registry,
        'pages': pages, 'reviews': ordered_reviews,
        'deferred': [r for r in manifest['items'] if r['logical_work']['run_id'] == run['run_id'] and r['state'] == 'deferred'],
        'source_tasks_completed': False}
    ref = (root(service, run)/(fingerprint(intent)+'.intent.json')).relative_to(service.vault).as_posix()
    return {'ref': ref, 'sha256': hashlib.sha256(_json_bytes(intent)).hexdigest()}, intent


def start(service, pointer, intent):
    path = _vault_path(service.vault, pointer['ref'])
    if path.exists() and _load_json(path) != intent:fail('partial intent identity collision')
    _write_atomic(path, _json_bytes(intent))


def finish(service, pointer, intent):
    verify_outputs(service, intent)
    receipt = expected_receipt(pointer, intent)
    _write_atomic(receipt_path(service, pointer), _json_bytes(receipt))
    return receipt


def original_parent(service, run, page, *, state=None):
    state = progress(service, run) if state is None else state
    candidates = list(state['committed'].values())
    if state['pending']:
        candidates.extend({'page': p, 'snapshot': state['pending'][1]['snapshot']} for p in state['pending'][1]['pages'])
    item = next((r for r in candidates if r['page']['page_id'] == page['page_id']), None)
    if not item:return None
    basis = pinned(service.vault, item['snapshot'])
    alias = next(a for a,p in basis['backend']['pages'].items() if p['run_id'] == run['run_id'] and p['page']['page_id'] == page['page_id'])
    view = next(v for v in basis['views'] if v['page'] == alias)
    parent = view['existing_page']
    if parent:
        record = _load_json(_vault_path(service.vault,
            f"_system/knowledge-builds/page-revisions/{page['page_id']}/{page['parent_revision_id']}.json"))
        if hashlib.sha256(parent['content'].encode()).hexdigest() != record['authored_sha256']:
            fail('captured original parent differs from its immutable revision', 'SOURCE_CHANGED')
    return {'existing_page': parent, 'parent_hash': basis['backend']['pages'][alias]['parent_hash']}


def equivalent_snapshot(service, previous, current):
    """Only authenticated partial registry transitions may differ on recovery."""
    old = copy.deepcopy(previous); old.pop('input_id', None)
    new = copy.deepcopy(current); new.pop('input_id', None)
    if old == new:return True
    from .finalize_revision_state import effective_run,active_for_run,content_path
    for run in old['backend']['runs']:
        effective=effective_run(service,run)
        if effective!=run:
            new_run=next((r for r in new['backend']['runs'] if r['run_id']==run['run_id']),None)
            if effective!=new_run:return False
            for alias,row in old['backend']['pages'].items():
                if row['run_id']!=run['run_id']:continue
                page=next(p for p in effective['page_revisions'] if p['page_id']==row['page']['page_id'])
                if page==row['page']:continue
                row['page']=page
                oview=next(v for v in old['views'] if v['page']==alias)
                oview['content']=content_path(service,effective,page).read_text(encoding='utf-8')
                from .build_acceptance import assess
                row['assessment']=assess(service,effective,page,parent_content=oview['existing_page']['content'] if oview['existing_page'] else None)
            run.clear();run.update(effective)
    # New operator scope metadata is permitted only through its verified chain;
    # it changes no candidate, material, identity or source coverage.
    for run in old['backend']['runs']:
        controls=active_for_run(service,run)
        for alias,row in old['backend']['pages'].items():
            if row['run_id']!=run['run_id']:continue
            work=controls.get(row['page']['page_id'])
            if work and work.get('grants'):
                grant=work['grants'][-1]
                view=next(v for v in old['views'] if v['page']==alias)
                view['finalize_revision']={'instruction':'Inspect only this explicitly reopened page issue, preserve assigned evidence/identity/coverage and retain unresolved limits. '+grant['basis']['reason'],
                    'basis_kind':grant['basis']['kind'],'predecessor_revision_id':work['original_revision_id']}
    for run in previous['backend']['runs']:
        state = progress(service, run)
        if not state['entries']:continue
        registries = [state['registry_before'], state['registry_after']]
        registries.extend(i['registry_before'] for _,i in state['entries'])
        registries.extend(i['registry_after'] for _,i in state['entries'])
        if old['backend']['registry'] in registries and new['backend']['registry'] in registries:
            old['backend']['registry'] = new['backend']['registry']
    return old == new


def assert_release_ready(service):
    """A direct release API cannot silently bypass an unfinished partial run."""
    for path in (service.vault/'_system/knowledge-builds').glob('*/partial-finalize/*.intent.json'):
        run = _load_json(path.parent.parent/'manifest.json')
        if run['state'] != 'completed':
            state = progress(service, run)
            if state['entries']:
                fail('unfinished partial Finalize retains page/source coverage gaps', 'INCOMPLETE_COVERAGE')
