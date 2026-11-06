"""Small mutable index, immutable payloads, operation-local verified reads."""
import copy,hashlib,json
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes,_write_atomic

INDEX='page-budget-index/v2'
WIRE='durable-page-calls/v1'
_READS=ContextVar('page_budget_operation',default=None)

def fail(message):raise ContractError('SOURCE_CHANGED','$',message)

@contextmanager
def operation():
    current=_READS.get()
    if current is not None:yield current;return
    value={'indexes':{},'payloads':{},'proofs':{},'index_loads':{},'payload_loads':{}}
    token=_READS.set(value)
    try:yield value
    finally:_READS.reset(token)

def load_index(root,*,fresh=False):
    p=Path(root)/'page-call-budget.json';scope=_READS.get();key=str(p)
    if not fresh and scope is not None and key in scope['indexes']:return scope['indexes'][key]
    value=json.loads(p.read_text()) if p.exists() else {'contract':INDEX,'version':0,'pages':{},'requests':{}}
    if value.get('contract') not in (INDEX,WIRE):fail('unknown page budget contract')
    if scope is not None:
        scope['index_loads'][key]=scope['index_loads'].get(key,0)+1
        if not fresh:scope['indexes'][key]=value
    return value

def target(root,ref):
    p=(Path(root)/ref).resolve()
    if not p.is_relative_to(Path(root).resolve()):fail('page payload escaped workflow')
    return p

def payload(root,pointer):
    p=target(root,pointer['ref']);scope=_READS.get();key=(str(p),pointer['sha256'])
    if scope is not None and key in scope['payloads']:return scope['payloads'][key]
    if scope is not None and any(k[0]==str(p) for k in scope['payloads']):fail('conflicting payload hashes in one operation')
    raw=p.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=pointer['sha256']:fail('immutable page payload changed')
    value=json.loads(raw)
    if scope is not None:
        scope['payload_loads'][str(p)]=scope['payload_loads'].get(str(p),0)+1
        scope['payloads'][key]=value
    return value

def put(root,kind,identity,value):
    ref=f'page-call-payloads/{kind}/{identity}.json';p=target(root,ref);raw=_json_bytes(value)
    pointer={'ref':ref,'sha256':hashlib.sha256(raw).hexdigest()}
    if p.exists():
        if payload(root,pointer)!=value:fail('immutable payload identity conflict')
    else:_write_atomic(p,raw)
    return pointer

def orphan(root,kind,identity):
    ref=f'page-call-payloads/{kind}/{identity}.json';p=target(root,ref)
    scope=_READS.get()
    if scope is not None:
        for key in scope['payloads']:
            if key[0]==str(p):return {'ref':ref,'sha256':key[1]}
    if not p.exists():return None
    # Deterministic crash recovery discovery. Cache these exact bytes too.
    raw=p.read_bytes();pointer={'ref':ref,'sha256':hashlib.sha256(raw).hexdigest()}
    scope=_READS.get()
    if scope is not None:
        key=(str(p),pointer['sha256'])
        if key not in scope['payloads']:
            scope['payloads'][key]=json.loads(raw)
            scope['payload_loads'][str(p)]=scope['payload_loads'].get(str(p),0)+1
    return pointer

class Snapshot:
    """Read-only scheduling/authentication input; never budget authority."""
    def __init__(self,root,index=None):self.root=Path(root);self.index=load_index(root) if index is None else index
    def row(self,key):
        row=self.index['pages'].get(key)
        if row is None:return None
        if self.index['contract']==WIRE:return row
        result=copy.deepcopy(row)
        if row.get('receipt'):result['receipt']=payload(self.root,row['receipt'])
        for a in result['attempts']:
            if a.get('failure_receipt'):a['failure_receipt']=payload(self.root,a['failure_receipt'])
        return result
    def request(self,identity):
        row=self.index['requests'][identity]
        if self.index['contract']==WIRE:return row
        result={**payload(self.root,row['payload']),'state':row['state']}
        pointer=row.get('response') or orphan(self.root,'responses',identity)
        if pointer:result['response']=payload(self.root,pointer)
        return result
    def full(self):
        return {'contract':WIRE,'pages':{k:self.row(k) for k in self.index['pages']},
            'requests':{k:self.request(k) for k in self.index['requests']}}

def snapshot(root):return Snapshot(root)
