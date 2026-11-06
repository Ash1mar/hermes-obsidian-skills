"""Opt-in, content-addressed Vault-data boundaries for same-workflow tests.

This captures data only, never installed code or host Provider indexes. Captures
do not grant execution/retry authority. A separate trusted test admission is
required to restore one after all consumers have stopped.
"""
import hashlib
from pathlib import Path

from .source_units import _load_json, _json_bytes, _write_atomic, _vault_path
from .validation import ContractError

CONTRACT = 'same-workflow-node-test/v1'
BOUNDARY = 'workflow-test-boundary/v1'
POLICY = '_system/metadata/workflow-test-policy.json'
NODES = ('bootstrap','source-prepare','exact-plan','canary','pass','resource-reduce',
         'global-reduce','checkpoint-1-validate','build-finalize','vault-finalize-plan',
         'checkpoint-2-validate','release-apply','provider-sync','acceptance')
STAGES={'source-prepare':('created','source_preparing'),'exact-plan':('planning',),
        'canary':('analyzing',),'pass':('analyzing',),'resource-reduce':('reducing',),
        'global-reduce':('reducing',),'checkpoint-1-validate':('reducing',),
        'build-finalize':('build_finalizing',),'vault-finalize-plan':('release_planning',),
        'checkpoint-2-validate':('release_planning','checkpoint_2'),'release-apply':('applying',),
        'provider-sync':('indexing',),'acceptance':('validating',)}


def fail(message):
    raise ContractError('WORKFLOW_TEST_BOUNDARY_INVALID', '$', message)


def root(vault, wid):
    return Path(vault)/f'_system/ledgers/ingest-workflows/{wid}/test-boundaries'


def stage(w, kind):
    if kind=='bootstrap':return 'source-prepare'
    if kind == 'pass-slice':
        return 'canary' if w['dispatch_policy']['mode'] == 'canary' else 'pass'
    if kind not in NODES:fail('unsupported workflow node')
    return kind


def audit_path(ref):
    parts=Path(ref).parts
    return (len(parts)>=5 and parts[:3]==('_system','ledgers','ingest-workflows')
            and parts[4] in ('test-boundaries','workflow-tests','reduce-tests'))


def inventory(vault):
    """One data read/hash per file per capture/inspection; no long-lived cache."""
    out={}
    import os
    for directory,dirs,names in os.walk(vault):
        parent=Path(directory)
        for name in [*dirs,*names]:
            if (parent/name).is_symlink():fail('symlink in test Vault')
        dirs[:]=sorted(d for d in dirs if not audit_path((parent/d).relative_to(vault).as_posix()))
        for name in sorted(names):
            p=parent/name;ref=p.relative_to(vault).as_posix()
            if p.suffix not in ('.lock','.tmp'):
                raw=p.read_bytes();out[ref]=(hashlib.sha256(raw).hexdigest(),raw)
    return out


def enable(vault, wid, actor):
    import os
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        fail('test capture requires a trusted operator')
    identity=_load_json(Path(vault)/'_system/vault.json')['vault']
    policy=_load_json(Path(vault)/'_system/metadata/retrieval-policy.json')
    if 'test' not in identity['security_domain'].lower() or policy.get('query_enabled') is not False:
        fail('test security domain and Query disabled required')
    value={'contract':CONTRACT,'workflow_id':wid,'actor':actor,'capture':True}
    path=Path(vault)/POLICY
    if path.exists() and _load_json(path)!=value:fail('test capture policy differs')
    if not path.exists():_write_atomic(path,_json_bytes(value))
    return value


def policy(vault,w):
    path=Path(vault)/POLICY
    if not path.exists():return None
    p=_load_json(path)
    if (p.get('contract')!=CONTRACT or p.get('workflow_id')!=w['workflow_id']
            or p.get('actor')!=w['actor'] or p.get('capture') is not True):
        fail('test capture policy identity differs')
    return p


def manifest(vault,wid,node,*,payloads=None):
    if node not in NODES:fail('unsupported workflow node')
    index=root(vault,wid)/'index.json'
    pointers=_load_json(index) if index.exists() else {}
    if node not in pointers:fail('no authenticated boundary for this historical node; no synthetic reset allowed')
    pointer=pointers[node];path=_vault_path(vault,pointer['ref']);raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=pointer['sha256']:fail('boundary descriptor changed')
    import json
    row=json.loads(raw)
    if (row.get('contract')!=BOUNDARY or row['workflow_id']!=wid or row['node']!=node
            or row['vault']!=str(Path(vault).resolve())):
        fail('foreign boundary identity')
    seen=set()
    import re
    payloads={} if payloads is None else payloads
    for ref,digest in row['files'].items():
        _vault_path(vault,ref)
        if not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest):fail('invalid payload identity')
        if digest in seen:continue
        seen.add(digest)
        blob=root(vault,wid)/'blobs'/digest
        if any(p.is_symlink() for p in (blob,*blob.parents)):fail('symlink in boundary payload')
        raw=blob.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=digest:
            fail('boundary data payload changed')
        payloads[digest]=raw
    return row,pointer['sha256']


def capture(adapter, workflow, node):
    """Called before native readiness. Return False while prefix runs drain."""
    if not policy(adapter.workflow.vault,workflow):return True
    from .source_units import _exclusive_lock
    from .runtime_telemetry import span
    service=adapter.workflow;vault=service.vault;wid=workflow['workflow_id']
    target=stage(workflow,node.kind);base=root(vault,wid)
    cycle=workflow.get('workflow_test',workflow.get('reduce_test',{})).get('operation_id','initial')
    path=base/'cycles'/cycle/(target+'.json')
    if path.exists():return True  # Immutable boundary, not an execution grant.
    with _exclusive_lock(service._lock(wid)):
        w=service.status(wid)
        if w['cancel_requested'] or service.pending_pause(w):return False
        if w['current_stage'] not in STAGES[target]:return False
        if path.exists():return True
        if w['batch_id']:
            slices=service.knowledge._slices(w['batch_id'])
            if any(s['lease']['worker_id'] or s['state']=='leased' for s in slices):return False
        # Capture a stable family boundary before its first runnable card. A
        # native prefix consumer may still be settling after its domain commit.
        native_prefix={}
        for card in w['kanban']['task_map']:
            snap=adapter.kanban.task_snapshot(w['kanban']['board_id'],card['task_id'])
            if snap['task'].get('worker_pid') or any(not r.get('ended_at') for r in snap.get('runs',[])):
                return False
            if snap['task'].get('status') in ('done','archived') and snap.get('runs'):
                native_prefix[card['task_id']]={'board_id':w['kanban']['board_id'],'snapshot':snap}
        with span('workflow_test_boundary_capture',vault=vault,workflow_id=wid,node=target) as trace:
            captured=inventory(vault)
            added=0;added_bytes=0
            for ref,(digest,raw) in captured.items():
                blob=base/'blobs'/digest
                if not blob.exists():
                    _write_atomic(blob,raw);added+=1;added_bytes+=len(raw)
            value={'contract':BOUNDARY,'workflow_id':wid,'actor':w['actor'],'vault':str(vault),
                'node':target,'workflow_revision':w['revision'],
                'cycle':cycle,
                'native_prefix':native_prefix,
                'files':{ref:digest for ref,(digest,raw) in captured.items()},
                'logical_vault_id':_load_json(vault/'_system/vault.json')['vault']['id'],
                'raw_sources':{ref:digest for ref,(digest,raw) in captured.items() if ref.startswith('10_Raw/')},
                'scope':'parallel node family and its downstream; completed prefix retained',
                'capture_grants_execution':False}
            _write_atomic(path,_json_bytes(value))
            index=_load_json(base/'index.json') if (base/'index.json').exists() else {}
            def point(name,p):index[name]={'ref':p.relative_to(vault).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            point(target,path)
            bootstrap=path.with_name('bootstrap.json')
            if target=='source-prepare' and not bootstrap.exists():
                _write_atomic(bootstrap,_json_bytes({**value,'node':'bootstrap',
                    'scope':'same-identity scaffold validation and source dispatch; never create another Vault'}))
                point('bootstrap',bootstrap)
            _write_atomic(base/'index.json',_json_bytes(index))
            trace.update(files=len(captured),logical_bytes=sum(len(raw) for _,raw in captured.values()),
                         new_blobs=added,new_blob_bytes=added_bytes)
    return True


def prefix_fingerprint(w,name):
    return next((r['fingerprint'] for r in w.get('workflow_test',{}).get('prefix_fingerprints',[]) if r['node']==name),None)


def check_admission(service,w):
    test=w.get('workflow_test')
    if not test:return
    if type(test['pass_capacity']) is not int or not 1<=test['pass_capacity']<=10:fail('invalid pinned Pass capacity')
    prefix={r['node']:r['fingerprint'] for r in test['prefix_fingerprints']}
    if len(prefix)!=len(test['prefix_fingerprints']):fail('duplicate prefix node identity')
    import json
    path=_vault_path(service.vault,test['report_ref']);raw=path.read_bytes();report=json.loads(raw)
    if (hashlib.sha256(raw).hexdigest()!=test['report_sha256']
            or report.get('contract')!=CONTRACT or report['request']['workflow_id']!=w['workflow_id']
            or report['request']['operation_id']!=test['operation_id']
            or report['request']['from_node']!=test['from_node']
            or report['request']['pass_capacity']!=test['pass_capacity']
            or report['prefix_fingerprints']!=prefix):
        fail('test admission identity changed')
    seal_path=path.parent/'admitted.json'
    seal=_load_json(seal_path) if seal_path.exists() else {}
    if (seal.get('phase')!='admitted' or seal.get('contract')!=CONTRACT
            or seal.get('workflow_id')!=w['workflow_id'] or seal.get('operation_id')!=test['operation_id']
            or seal.get('input_digest')!=report['request']['input_digest']
            or seal.get('report_ref')!=test['report_ref'] or seal.get('report_sha256')!=test['report_sha256']):
        fail('finish the exact pending node test admission before dispatch')
