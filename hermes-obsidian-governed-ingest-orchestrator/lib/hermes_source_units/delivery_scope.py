"""Hash-bound delivery closure; exclusions never become successful domain work."""
import copy,re
from pathlib import Path
from urllib.parse import unquote
from .source_units import _load_json,_vault_path,_write_atomic,_json_bytes
from .validation import ContractError,fingerprint
from .partial_finalize import pin,pinned

CONTRACT='finalize-delivery-scope/v1'
CLOSURE='finalize-delivery-closure/v1'

def fail(message,code='DELIVERY_SCOPE_INVALID'):
    raise ContractError(code,'$',message)

def active(service,run):
    from .finalize_revision_state import active_for_run
    return active_for_run(service,run).get('*',{}).get('delivery_scope')

def authorized(service,pointer):
    from .finalize_revision_state import load,work_id
    value=pinned(service.vault,pointer)
    if value.get('contract')!=CONTRACT or value.get('scope_id')!=fingerprint({k:v for k,v in value.items() if k!='scope_id'}):
        fail('delivery scope identity changed','SOURCE_CHANGED')
    expected=f"_system/ledgers/ingest-workflows/{value['workflow_id']}/delivery-scopes/{value['scope_id']}.json"
    if pointer['ref']!=expected:fail('delivery scope path differs from its identity')
    basis=pinned(service.vault,value['snapshot']);pages={(r['run_id'],p['page_id']):p for r in basis['backend']['runs'] for p in r['page_revisions']}
    partition=value['included']+value['excluded'];keys=[(p['run_id'],p['page_id']) for p in partition]
    if not value['included'] or len(keys)!=len(set(keys)) or set(keys)!=set(pages):fail('delivery scope lost or duplicated original pages')
    for p in value['included']:
        original=pages[(p['run_id'],p['page_id'])]
        if any(p[k]!=original[k] for k in ('revision_id','authored_sha256','path')):fail('delivery scope page differs from its input')
    state=load(service.vault,value['workflow_id'])
    for row in value['runs']:
        key=work_id(row['run_id'],'*')
        if not any(e['work']['work_id']==key and e['work'].get('delivery_scope')==pointer
                and e['metadata'].get('actor')==value['actor'] for e in state['events']):
            fail('delivery scope lacks the owning stopped-operator authorization','ACCESS_DENIED')
    return value

def closure_path(vault,pointer):
    path=_vault_path(vault,pointer['ref'])
    return path.with_name(path.stem+'.closure.json')

def dependency_partition(pages,excluded):
    """Resolve declared page links, then exclude their transitive dependents.

    Shared source evidence is not a page dependency. Link identity is resolved
    from the complete page set, never a document-specific title whitelist.
    """
    lookup={};by_id={p['page_id']:p for p in pages};edges={p:set() for p in by_id}
    for p in pages:
        for key in (p['path'],p['path'].removesuffix('.md'),Path(p['path']).stem,
                    p['identity']['canonical_name'],*p['identity'].get('aliases',[])):
            lookup.setdefault(key.casefold(),set()).add(p['page_id'])
    reasons=copy.deepcopy(excluded)
    for p in pages:
        links=[s.split('|',1)[0].split('#',1)[0].strip() for s in re.findall(r'\[\[([^\]]+)\]\]',p['content'])]
        for raw in re.findall(r'(?<!!)\[[^\]]*\]\(([^)]+)\)',p['content']):
            target=unquote(raw.strip().strip('<>').split('#',1)[0])
            if target.lower().endswith('.md') and not re.match(r'^[a-zA-Z]+:',target):
                import posixpath
                links.append(posixpath.normpath(posixpath.join(str(Path(p['path']).parent),target)))
        for link in filter(None,links):
            matches=lookup.get(unquote(link).casefold(),set())
            if len(matches)==1:edges[p['page_id']].update(matches-{p['page_id']})
            elif not matches or len(matches)>1:
                reasons.setdefault(p['page_id'],{'category':'dependency_unresolved','reason':'page link lacks a unique authorized page identity','links':[],'reopen_condition':'resolve and verify the page dependency under a new delivery scope'})
                reasons[p['page_id']].setdefault('links',[]).append(link)
    changed=True
    while changed:
        changed=False
        for pid,deps in edges.items():
            if pid in reasons or not deps.intersection(reasons):continue
            reasons[pid]={'category':'dependent_exclusion','reason':'depends on excluded page content',
                'depends_on':sorted(deps.intersection(reasons)),
                'reopen_condition':'resolve the dependency and authorize a new delivery scope'};changed=True
    return {'included':sorted(set(by_id)-set(reasons)),'excluded':reasons,
        'dependencies':{k:sorted(v) for k,v in edges.items() if v}}

def guard_page(service,wid,path):
    """A late reply cannot re-enter the selected delivery through another ID."""
    from .finalize_revision_state import load
    state=load(service.vault,wid)
    for work in state['works'].values():
        pointer=work.get('delivery_scope')
        if not pointer:continue
        scope=authorized(service,pointer)
        if any(p['path']==path for p in scope['excluded']):
            if reopened_after(service,scope,path):continue
            fail('this page remains quarantined outside the authorized delivery','DELIVERY_SCOPE_EXCLUDED')

def reopened_after(service,scope,path):
    from .finalize_revision_state import load,work_id
    item=next((p for p in scope['excluded'] if p['path']==path),None)
    if not item:return False
    work=load(service.vault,scope['workflow_id'])['works'].get(work_id(item['run_id'],item['page_id']),{})
    return bool(work.get('grants') and work['grants'][-1].get('authorized_control_revision',0)>scope.get('control_revision',10**12))

def pending_reopen(service,run):
    pointer=active(service,run)
    if not pointer:return False
    scope=authorized(service,pointer)
    return any(reopened_after(service,scope,p['path']) for p in scope['excluded'])

def guard_selection(service,run,page_ids):
    pointer=active(service,run)
    if not pointer:return
    scope=authorized(service,pointer)
    excluded={p['page_id'] for p in scope['excluded'] if p['run_id']==run['run_id'] and not reopened_after(service,scope,p['path'])}
    if excluded.intersection(page_ids):fail('Finalize cannot submit excluded pages','DELIVERY_SCOPE_EXCLUDED')

def verify(service,pointer,*,historical=False):
    """Verify a native closure; historical releases retain their exact scope."""
    scope=authorized(service,pointer)
    path=closure_path(service.vault,pointer)
    value=_load_json(path)
    if (value.get('contract')!=CLOSURE or value.get('authorization')!=pointer
            or value.get('closure_id')!=fingerprint({k:v for k,v in value.items() if k!='closure_id'})
            or value['excluded']!=scope['excluded'] or value['coverage_gaps']!=scope['coverage_gaps']
            or value.get('coverage_complete') is not False or value.get('delivery_complete') is not True):
        fail('closure differs from its exact authorized exclusions','SOURCE_CHANGED')
    expected={(p['run_id'],p['page_id'],p['revision_id'],p['authored_sha256']) for p in scope['included']}
    actual={(p['run_id'],p['page']['page_id'],p['page']['revision_id'],p['page']['authored_sha256']) for p in value['pages']}
    if expected!=actual or len(actual)!=len(value['pages']):fail('closure page accounting differs')
    for row in scope['runs']:
        if pin(service.vault,row['manifest']['ref'])!=row['manifest']:fail('original Build manifest changed','SOURCE_CHANGED')
        run=_load_json(service._run_path(row['run_id']))
        if not historical and active(service,run)!=pointer:fail('delivery scope was superseded','STALE_INPUT')
        if not historical:
            for task in row['tasks']:
                if pin(service.vault,task['ref'])!=task:fail('uncompleted source task changed','SOURCE_CHANGED')
    for row in value['pages']:
        p=row['page'];side=f"_system/knowledge-builds/page-revisions/{p['page_id']}/{p['revision_id']}.json"
        if _load_json(_vault_path(service.vault,side))!=p:fail('closed page revision changed','SOURCE_CHANGED')
        if not historical:
            import hashlib
            if hashlib.sha256(_vault_path(service.vault,p['path']).read_bytes()).hexdigest()!=p['authored_sha256']:
                fail('closed canonical page bytes changed','SOURCE_CHANGED')
    return value

def close(service,binding,snapshot):
    """Native-only mechanical closure of existing authenticated canonical pages."""
    from .partial_finalize import authority,progress,equivalent_snapshot
    from .build_acceptance import validate_acceptance
    if authority(service,binding['actor'])!=binding:fail('native closure binding differs','ACCESS_DENIED')
    runs=snapshot['backend']['runs'];pointers=[active(service,r) for r in runs]
    if not pointers or any(p!=pointers[0] for p in pointers) or not pointers[0]:fail('all current runs require one exact delivery authorization')
    pointer=pointers[0];scope=authorized(service,pointer)
    if scope['workflow_id']!=binding['workflow_id'] or scope['actor']!=binding['actor']:fail('foreign delivery owner','ACCESS_DENIED')
    original=pinned(service.vault,scope['snapshot'])
    current=copy.deepcopy(snapshot);current['binding']=original['binding']
    if not equivalent_snapshot(service,original,current):fail('scope domain evidence changed','STALE_INPUT')
    pages=[]
    for run in runs:
        service.validate_run(run['run_id']);state=progress(service,run)
        if state['pending']:fail('finish the exact interrupted commit before closing')
        candidates,_=service._pass_index([t['task_id'] for t in run['tasks']])
        decisions={d['page_id']:d for d in run['decisions']}
        wanted={p['page_id'] for p in scope['included'] if p['run_id']==run['run_id']}
        for pid in sorted(wanted):
            committed=state['committed'].get(pid)
            if not committed:fail('included page lacks a verified native commit')
            selected=[candidates.get((r['pass_id'],r['candidate_id'])) for r in decisions[pid]['candidate_refs']]
            validate_acceptance(service,run,committed['page'],committed['review'],selected=selected)
            pages.append({'run_id':run['run_id'],'page':committed['page']})
    value={'contract':CLOSURE,'authorization':pointer,'workflow_id':scope['workflow_id'],
        'outcome':'closed_with_deferrals','coverage_complete':False,'delivery_complete':True,
        'pages':pages,'excluded':scope['excluded'],'coverage_gaps':scope['coverage_gaps'],
        'source_tasks_completed':False,'new_domain_calls':0}
    value['closure_id']=fingerprint(value);path=closure_path(service.vault,pointer)
    if path.exists():
        if _load_json(path)!=value:fail('native scope closure already differs','SOURCE_CHANGED')
    else:_write_atomic(path,_json_bytes(value))
    verify(service,pointer)
    return pin(service.vault,path.relative_to(service.vault).as_posix())

def completed(service,run):
    if run['state']=='completed':return True
    pointer=active(service,run)
    if not pointer or not closure_path(service.vault,pointer).exists():return False
    verify(service,pointer);return True

def batch_complete(service,batch):
    return bool(batch['run_ids']) and all(completed(service,_load_json(service._run_path(r))) for r in batch['run_ids'])

def closure_refs(service,run_ids):
    result=[]
    for rid in run_ids:
        run=_load_json(service._run_path(rid));pointer=active(service,run)
        if not pointer:continue
        verify(service,pointer)
        p=pin(service.vault,closure_path(service.vault,pointer).relative_to(service.vault).as_posix())
        if p not in result:result.append(p)
    return result

def release_closures(service,pointers,run_ids,*,historical=False):
    result={}
    for pointer in pointers:
        closure=pinned(service.vault,pointer)
        actual=verify(service,closure['authorization'],historical=historical)
        if actual!=closure:fail('release closure pin differs','SOURCE_CHANGED')
        scope=authorized(service,closure['authorization'])
        for row in scope['runs']:
            if row['run_id'] not in run_ids:fail('release scope contains an unselected run')
            if row['run_id'] in result:fail('release scopes overlap')
            result[row['run_id']]={'scope':scope,'closure':closure,'pointer':pointer}
    return result
