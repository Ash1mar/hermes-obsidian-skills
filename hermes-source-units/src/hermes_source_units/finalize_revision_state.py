"""Finalize operator decisions and cumulative call ordinals, independent of IDs.

All updates append immutable events; a small head points to their verified chain.
Revision bodies are native outputs and retain the original page/run predecessor.
"""
import copy,hashlib
from .source_units import _load_json,_vault_path,_write_atomic,_json_bytes,_exclusive_lock
from .validation import ContractError,fingerprint,validate_record
from .partial_finalize import pin,pinned

CONTRACT='finalize-disposition-control/v1'

def fail(message,code='FINALIZE_DISPOSITION_INVALID'):
    raise ContractError(code,'$',message)

def directory(vault,wid):
    return _vault_path(vault,f'_system/ledgers/ingest-workflows/{wid}/finalize-controls')

def work_id(run_id,page_id):return fingerprint({'run_id':run_id,'page_id':page_id})

def load(vault,wid):
    d=directory(vault,wid);path=d/'state.json'
    if not path.exists():return {'revision':0,'head':None,'works':{},'events':[]}
    state=_load_json(path)
    if state.get('contract')!=CONTRACT or state.get('workflow_id')!=wid:fail('foreign Finalize control state')
    pointer=state['head'];reverse=[];seen=set()
    while pointer:
        if pointer['ref'] in seen:fail('cyclic Finalize control history')
        seen.add(pointer['ref']);event=pinned(vault,pointer)
        if (event.get('contract')!=CONTRACT or event.get('workflow_id')!=wid
                or _vault_path(vault,pointer['ref']).parent!=d/'events'
                or _vault_path(vault,pointer['ref']).stem!=fingerprint(event)):
            fail('Finalize control event identity changed','SOURCE_CHANGED')
        reverse.append(event);pointer=event['previous']
    if {p.relative_to(vault).as_posix() for p in (d/'events').glob('*.json')}!=seen:
        fail('Finalize control head does not cover all durable events; stop for recovery','SOURCE_CHANGED')
    events=list(reversed(reverse));works={}
    for n,event in enumerate(events,1):
        work=event['work']
        if event['revision']!=n or work['work_id']!=work_id(work['run_id'],work['page_id']):fail('Finalize control sequence differs')
        works[work['work_id']]=work
    if state['revision']!=len(events):fail('Finalize control head revision differs')
    return {**state,'works':works,'events':events}

def append(vault,wid,work,operation_id,kind,metadata):
    d=directory(vault,wid)
    with _exclusive_lock(d/'.lock'):
        state=load(vault,wid)
        old=next((e for e in state['events'] if e['operation_id']==operation_id),None)
        if old:
            if old['kind']!=kind or old['metadata']!=metadata:fail('Finalize operation ID is already bound','IDEMPOTENCY_CONFLICT')
            return state,old
        event={'contract':CONTRACT,'workflow_id':wid,'revision':state['revision']+1,
            'previous':state['head'],'operation_id':operation_id,'kind':kind,'metadata':metadata,'work':work}
        ref=(d/'events'/f'{fingerprint(event)}.json').relative_to(vault).as_posix()
        _write_atomic(_vault_path(vault,ref),_json_bytes(event))
        _write_atomic(d/'state.json',_json_bytes({'contract':CONTRACT,'workflow_id':wid,
            'revision':event['revision'],'head':pin(vault,ref)}))
        return load(vault,wid),event

def active_for_run(service,run):
    found={}
    for path in (service.vault/'_system/ledgers/ingest-workflows').glob('*/finalize-controls/state.json'):
        state=load(service.vault,path.parent.parent.name)
        for work in state['works'].values():
            if work['run_id']==run['run_id']:
                if work['page_id'] in found:fail('multiple workflows own a Finalize page')
                found[work['page_id']]=work
    return found

def effective_run(service,run):
    if run['state']=='completed':return copy.deepcopy(run)
    result=copy.deepcopy(run)
    for pid,work in active_for_run(service,run).items():
        pointer=work.get('latest_revision')
        if not pointer:continue
        revision=pinned(service.vault,pointer)
        original=next(p for p in run['page_revisions'] if p['page_id']==pid)
        if (revision.get('contract')!='finalize-local-page-revision/v1'
                or revision['run_id']!=run['run_id'] or revision['original_page']!=original
                or revision['work_id']!=work['work_id']):
            fail('Finalize revision is not bound to its original page','SOURCE_CHANGED')
        page=revision['page'];validate_record('page_revision',page)
        if any(page[k]!=original[k] for k in ('page_id','subject_id','parent_revision_id','action','path','support_refs','qa_status','business_status','visibility')):
            fail('local Finalize revision changed identity, coverage or governance')
        content=_vault_path(service.vault,revision['content']['ref']).read_bytes()
        if hashlib.sha256(content).hexdigest()!=revision['content']['sha256'] or page['authored_sha256']!=revision['content']['sha256']:
            fail('native revision body changed','SOURCE_CHANGED')
        index=next(i for i,p in enumerate(result['page_revisions']) if p['page_id']==pid)
        result['page_revisions'][index]=page
        decision=next(d for d in result['decisions'] if d['page_id']==pid);decision['revision_id']=page['revision_id']
    return result

def content_path(service,run,page):
    path=service._run_path(run['run_id']).parent/'pages'/f"{page['page_id']}.md"
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==page['authored_sha256']:
        return path
    work=active_for_run(service,run).get(page['page_id'])
    if not work or not work.get('latest_revision'):
        # Keep the ordinary validators' original draft-drift error contract.
        # Returning its path never accepts changed bytes: every caller checks
        # the recorded authored hash before using the content.
        return path
    revision=pinned(service.vault,work['latest_revision'])
    if {k:v for k,v in revision['page'].items() if k not in ('state','review')}!={k:v for k,v in page.items() if k not in ('state','review')}:
        fail('revised page differs from registered native output','SOURCE_CHANGED')
    return _vault_path(service.vault,revision['content']['ref'])

def publish_revision(service,binding,work,content,audit_pointer,view_pointer):
    from .partial_finalize import authority
    authority(service,binding['actor'])
    origin=_load_json(service._run_path(work['run_id']))
    from .partial_finalize import progress
    if work['page_id'] in progress(service,origin)['committed']:
        fail('local pending-page revision cannot replace an already committed valid page')
    audit=pinned(service.vault,audit_pointer)
    call=work['grants'][-1]['calls'].get('page-write')
    if (not call or call['state']!='valid' or call['audit']!=audit_pointer
            or audit.get('native_task_id')!=binding['task_id'] or audit.get('node')!='build-finalize'
            or audit.get('phase')!='page-write' or audit.get('model',{}).get('origin')!='hermes_native_model'
            or audit.get('response_sha256')!=audit.get('model',{}).get('response_sha256')):
        fail('local body revision lacks its consumed native call proof')
    if call.get('response',{}).get('content')!=content or call.get('request')!=view_pointer:
        fail('local body bytes differ from the authenticated native call result')
    original=next(p for p in origin['page_revisions'] if p['page_id']==work['page_id'])
    raw=(content.replace('\r\n','\n').replace('\r','\n').rstrip()+'\n').encode()
    if not raw.strip():fail('empty native revision')
    page={**original,'authored_sha256':hashlib.sha256(raw).hexdigest()}
    page['revision_id']=fingerprint({k:page[k] for k in ('page_id','subject_id','parent_revision_id','action','path',
        'authored_sha256','support_refs','qa_status','business_status','visibility')})
    if page['revision_id']==original['revision_id'] and not work.get('latest_revision'):
        return None  # A native unchanged body consumes its ordinal, not a fabricated content revision.
    previous=work.get('latest_revision')
    predecessor=pinned(service.vault,previous)['page']['revision_id'] if previous else original['revision_id']
    revision_dir=service._run_path(work['run_id']).parent/'finalize-revisions'
    body=revision_dir/f"{page['revision_id']}.md"
    if body.exists() and body.read_bytes()!=raw:fail('revision body identity collision')
    _write_atomic(body,raw)
    record={'contract':'finalize-local-page-revision/v1','work_id':work['work_id'],'run_id':work['run_id'],
        'original_page':original,'predecessor_revision_id':predecessor,'predecessor':previous,
        'page':page,'content':pin(service.vault,body.relative_to(service.vault).as_posix()),
        'native_audit':audit_pointer,'request':view_pointer,'grant_id':work['grants'][-1]['grant_id']}
    path=revision_dir/f'{fingerprint(record)}.json';_write_atomic(path,_json_bytes(record))
    return pin(service.vault,path.relative_to(service.vault).as_posix())

def original_pages_for_scope(service,run):
    """Keep the original limited-scope whitelist after native body revisions."""
    pages=copy.deepcopy(run['page_revisions'])
    for pid,work in active_for_run(service,run).items():
        if not work.get('latest_revision'):continue
        revision=pinned(service.vault,work['latest_revision'])
        current=next((p for p in pages if p['page_id']==pid),None)
        if current and current['revision_id']==revision['page']['revision_id']:
            if run['state']=='completed' and (work.get('review_result',{}).get('decision')!='approved'):
                fail('completed native revision lacks its independent approval')
            pages[pages.index(current)]=revision['original_page']
    return pages
