"""Durable fencing and cumulative budgets for one bounded citation amendment."""
import copy
import os
import hashlib
from pathlib import Path
from .source_units import _load_json,_vault_path,_write_atomic,_json_bytes,_exclusive_lock
from .validation import ContractError,fingerprint

SCOPE='reduce-citation-revision/v1'
PHASES=('prepared','citation','successor_verified','downstream','awaiting_acceptance','complete')

def fail(message):raise ContractError('CITATION_AMENDMENT_HELD','$',message)
def basis_key(items):return fingerprint(sorted({b['sha256'] for b in items}))


def lineage_limits(ledger,lineage,initial):
    """Explicit append-only extensions never erase the initial finite limits."""
    limits=copy.deepcopy(ledger.get('limits',{}).get(lineage,initial))
    caps={'citation':2,'source':2,'plan':2,'body':4}
    for extension in ledger.get('limit_extensions',[]):
        if extension['lineage']!=lineage:continue
        phase=extension['phase'];new=extension['cumulative_limit']
        if (extension['previous_limit']!=limits[phase] or type(new) is not int
                or new!=limits[phase]+1 or new>caps[phase] or extension.get('additional_calls')!=1
                or not extension.get('basis') or not extension.get('reason')):
            fail('cumulative extension is not a finite explicit successor')
        limits[phase]=new
    return limits

def root(vault,wid):return _vault_path(vault,f'_system/ledgers/ingest-workflows/{wid}/citation-amendments')
def state(vault,wid):
    base=root(vault,wid)
    if (base/'pending.json').exists():fail('finish the pending amendment transaction before dispatch')
    path=base/'active.json'
    return _load_json(path) if path.exists() else None

def plan(vault,value):
    path=_vault_path(vault,value['plan_ref'])
    if hashlib.sha256(path.read_bytes()).hexdigest()!=value['plan_sha256']:fail('amendment plan changed')
    frozen=_load_json(path)
    for b in frozen.get('basis',[]):
        if hashlib.sha256(_vault_path(vault,b['ref']).read_bytes()).hexdigest()!=b['sha256']:fail('pinned amendment cause changed')
    if frozen.get('old_pass_ref') and hashlib.sha256(_vault_path(vault,frozen['old_pass_ref']).read_bytes()).hexdigest()!=frozen['old_pass_sha256']:
        fail('immutable citation predecessor changed')
    return frozen

def transaction(vault,wid,writes,identity):
    """Persist all intents first. Partial control writes always fence consumers."""
    base=root(vault,wid);pending=base/'pending.json'
    intent={'identity':identity,'writes':writes}
    audit=base/'transactions'/(fingerprint(intent)+'.json')
    if not audit.exists():_write_atomic(audit,_json_bytes(intent))
    if pending.exists():
        if _load_json(pending)!=intent:fail('replay the exact pending operation')
    else:_write_atomic(pending,_json_bytes(intent))
    for row in writes:
        target=_vault_path(vault,row['ref']);raw=_json_bytes(row['value'])
        current=hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        if current==hashlib.sha256(raw).hexdigest():continue
        if current!=row['previous_sha256']:fail('amendment transaction control changed')
        _write_atomic(target,raw)
    pending.unlink()

def replay(vault,wid):
    path=root(vault,wid)/'pending.json'
    if path.exists():
        data=_load_json(path);transaction(vault,wid,data['writes'],data['identity'])

def row(vault,path,value):
    return {'ref':path.relative_to(Path(vault)).as_posix(),'value':value,
            'previous_sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None}

def save(vault,wid,value):
    base=root(vault,wid);path=base/'active.json'
    event={'previous_sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None,
           'value':value}
    _write_atomic(base/'events'/(fingerprint(event)+'.json'),_json_bytes(event))
    _write_atomic(path,_json_bytes(value))

def guard(vault,wid,node=None,*,build=False,domain=False):
    value=state(vault,wid)
    if not value or value['phase']=='complete':return
    if build or node and node.partition(':')[0] in ('build-finalize','checkpoint-2-validate','release-prepare','provider-sync','acceptance'):
        fail('citation amendment must be reaccepted at checkpoint_1 before Build')
    if node:
        allowed=(node.startswith('pass-slice:') and value['phase']=='citation'
                 or node in value.get('downstream_nodes',[]) and value['phase']=='downstream'
                 or node in ('global-reduce','checkpoint-1-validate') and value['phase']=='awaiting_acceptance')
        if not allowed:fail('old or unrelated executor is fenced by the citation amendment')
        if domain and node=='global-reduce' and value['phase']=='awaiting_acceptance':
            from .ingest_workflow import FileIngestWorkflowService
            from .reduce_coverage import active
            scope=active(vault,FileIngestWorkflowService(vault).status(wid))
            if not scope or scope['ref']['manifest_sha256']!=value['manifest_sha256']:
                fail('new limited acceptance must be admitted before a global domain commit')

def revision_authority(service,workflow,request):
    value=state(service.vault,workflow['workflow_id'])
    if not value or value['phase']!='citation' or request.get('amendment_id')!=value['operation_id']:
        fail('consumed revision requires the active prepared amendment')
    frozen=plan(service.vault,value);selected=frozen['selection']
    if (request['tasks']!=[{'task_id':selected['task_id'],'pass_id':selected['pass_id'],
                           'reason':frozen['reason']}]
            or request['revision_id']!=value['revision_id']):fail('citation revision scope differs from its plan')
    batch=service.knowledge._batch(workflow['batch_id'])
    if batch['run_ids'] or batch.get('global_reduction_id') or any(
            r in batch['resource_reduction_ids'] for r in frozen['retired_source_ids']):
        fail('retire dependent control pointers before native citation mutation')
    return frozen

def check_successor(knowledge,revision,prior,body):
    # The active write context supplies the authoritative workflow identity.
    from .workflow_guard import _ACTIVE
    context=_ACTIVE.get()
    if not context:return
    current=state(knowledge.vault,context['workflow_id'])
    if not current or current['phase']!='citation':return
    frozen=plan(knowledge.vault,current)
    if revision['revision_id']!=current['revision_id']:fail('stale citation revision executor')
    from .citation_revision_impact import successor_delta
    successor_delta(prior,body,frozen['selection']['candidate_ids'])
    sources={r['unit_ref']['resource_id'] for r in knowledge._task(body['task_id'])['target_refs']}
    if any(r['unit_ref']['resource_id'] not in sources for c in body['candidates'] for r in c['support_refs']):
        fail('native citation expanded the original source authority')
    from citation_amendment_calls import verify_pending_submission
    verify_pending_submission(knowledge.vault,context['workflow_id'],body)

def reserve(vault,wid,logical,wire,schema,binding,images=None):
    """One durable reservation spans input hashes, versions and operation IDs."""
    base=root(vault,wid)
    with _exclusive_lock(base/'budget.lock'):
        value=state(vault,wid)
        if not value or value['phase'] not in ('citation','downstream','awaiting_acceptance'):fail('amendment is not executable')
        frozen=plan(vault,value);phase=logical.partition(':')[0]
        if phase not in frozen['budgets']:fail('phase has no explicit call budget')
        path=base/'budgets.json';ledger=_load_json(path)
        job=ledger['jobs'].setdefault(logical,{'attempts':[]})
        owned=[a for a in job['attempts'] if a['operation_id']==value['operation_id']]
        digest=fingerprint(wire);grants=value.get('retry_grants',{}).get(logical,[])
        for grant in grants:
            for item in grant['basis']:
                if hashlib.sha256(_vault_path(vault,item['ref']).read_bytes()).hexdigest()!=item['sha256']:
                    fail('retry cause changed after authorization')
        may_append=bool(owned and len(owned)==len(grants) and
                        grants[-1]['supersedes_attempt_sha256']==fingerprint(owned[-1]))
        if owned and (owned[-1]['wire_sha256']!=digest or owned[-1]['schema_sha256']!=fingerprint(schema)):
            from citation_amendment_calls import guidance_equivalent
            if not (may_append and grants[-1].get('guidance') and owned[-1]['schema_sha256']==fingerprint(schema)
                    and guidance_equivalent(owned[-1]['wire'],wire,grants[-1]['guidance'])):
                fail('an input/version change cannot create another automatic logical allowance')
        if owned and not may_append:
            if owned[-1]['wire_sha256']!=digest or owned[-1]['schema_sha256']!=fingerprint(schema):
                fail('logical work already reserved under a different input; no automatic new allowance')
            return path,ledger,logical,len(job['attempts'])-1,True
        if value['phase']=='awaiting_acceptance':fail('acceptance proof may replay native evidence but cannot make a fresh call')
        used=sum(a['operation_id']==value['operation_id'] for j in ledger['jobs'].values() for a in j['attempts']
                 if a['phase']==phase)
        extra=sum(len(g) for key,g in value.get('retry_grants',{}).items() if key.partition(':')[0]==phase)
        if used>=frozen['budgets'][phase]+extra:fail('explicit amendment phase budget exhausted')
        # Grant lineage is tied to original task, never to a changing Pass hash.
        lineage=frozen['selection']['task_id']
        cumulative=sum(a['lineage']==lineage and a['phase']==phase for j in ledger['jobs'].values() for a in j['attempts'])
        if cumulative>=lineage_limits(ledger,lineage,frozen['cumulative_limits'])[phase]:fail('cumulative amendment lineage budget exhausted')
        attempt={'operation_id':value['operation_id'],'lineage':lineage,'phase':phase,
                 'wire_sha256':digest,'schema_sha256':fingerprint(schema),'wire':wire,'schema':schema,
                 'binding':dict(binding),'state':'reserved','audit_ref':None,'session_id':None}
        attempt.update(native_run_id=os.environ.get('HERMES_KANBAN_RUN_ID'),images=images or [],
                       events=[{'state':'reserved','wire_sha256':digest,'schema_sha256':fingerprint(schema)}])
        job['attempts'].append(attempt);_write_atomic(path,_json_bytes(ledger))
        return path,ledger,logical,len(job['attempts'])-1,False

def update_call(reservation,**fields):
    path,_,logical,index,_=reservation
    with _exclusive_lock(path.parent/'budget.lock'):
        ledger=_load_json(path);record=ledger['jobs'][logical]['attempts'][index]
        if set(fields)&{'wire','wire_sha256','schema','schema_sha256','binding','native_run_id','operation_id','lineage','phase','events'}:
            fail('reservation identity and final request are immutable')
        if all(record.get(k)==v for k,v in fields.items()):return
        previous=fingerprint(record)
        record.setdefault('events',[]).append({'previous_sha256':previous,'previous_state':record['state'],
                                               'changes':fields})
        record.update(fields)
        _write_atomic(path,_json_bytes(ledger))


def claim_call(reservation,wire,schema,binding,run_id):
    """A durable claim precedes any SDK work; uncertainty never refunds it."""
    path,_,logical,index,_=reservation
    with _exclusive_lock(path.parent/'budget.lock'):
        ledger=_load_json(path);record=ledger['jobs'][logical]['attempts'][index]
        continued=record.get('continuation',{})
        owner_matches=(record['binding']==binding and str(record.get('native_run_id'))==str(run_id))
        if continued:
            from citation_amendment_control import contract_hash
            owner_matches=(continued['contract_sha256']==contract_hash() and record['binding']['node']==binding['node']
                and record['binding']['workflow_id']==binding['workflow_id'] and record['binding']['task_id']!=binding['task_id'])
        if (record['wire_sha256']!=fingerprint(wire) or record['schema_sha256']!=fingerprint(schema) or not owner_matches):
            fail('dispatch identity differs from its fixed reservation')
        if record['state']!='reserved' or record.get('session_id') or record.get('audit_ref'):
            fail('reservation already claimed; observe or authenticate the original execution')
        if not record.get('events'):fail('legacy reservation has no dispatch certification')
        record['events'].append({'previous_sha256':fingerprint(record),'previous_state':record['state'],
                                 'state':'dispatch_claimed','binding':dict(binding),'native_run_id':run_id})
        record['state']='dispatch_claimed'
        record['execution_binding']=dict(binding);record['execution_run_id']=run_id
        _write_atomic(path,_json_bytes(ledger))


def continue_call(reservation,contract_sha256,ended_run_ids):
    """Continue a certified untouched reservation, retaining its charged ordinal."""
    path,_,logical,index,_=reservation
    with _exclusive_lock(path.parent/'budget.lock'):
        ledger=_load_json(path);record=ledger['jobs'][logical]['attempts'][index]
        if (record['state']!='reserved' or record.get('session_id') or record.get('audit_ref')
                or not record.get('events') or any(e.get('state')=='dispatch_claimed' for e in record['events'])):
            fail('reservation is unknown or dispatched; continuation cannot refund or regenerate it')
        continuation={'contract_sha256':contract_sha256,'ended_run_ids':ended_run_ids,'additional_calls':0}
        if record.get('continuation')==continuation:return
        if record.get('continuation'):fail('continue the existing reservation authority')
        record['events'].append({'previous_sha256':fingerprint(record),'continuation':continuation})
        record['continuation']=continuation
        _write_atomic(path,_json_bytes(ledger))

def pending_calls(vault,wid):
    path=root(vault,wid)/'budgets.json'
    return _load_json(path) if path.exists() else {'jobs':{}}
