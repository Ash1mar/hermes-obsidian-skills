"""Operation-local pure JSON/shape reuse, always gated by freshly read bytes."""
import copy
import hashlib
import json
import threading
import time
import sys
from collections.abc import Mapping
from collections import OrderedDict
from contextlib import nullcontext
from functools import wraps
from pathlib import Path

from .validation import ContractError


class VerifiedJsonReads:
    def __init__(self, limit=128):
        self.limit=limit;self.entries=OrderedDict();self.lock=threading.RLock()
        self.schema_sha=None
        self.stats={name:0 for name in ('reads','bytes','parses','validations','hits','projections',
            'read_hash_ns','parse_ns','validation_ns','projection_ns','copy_ns','index_ns',
            'index_builds','full_copies','projected_copies','copied_value_nodes','full_copy_ns','projected_copy_ns',
            'full_copy_nodes','projected_copy_nodes','snapshot_reads','freeze_ns','canonical_requests',
            'canonical_builds','canonical_hits','canonical_ns','immutable_versions')}
        self.copy_origins={}
        self.snapshot_origins={}
        self.snapshot_versions=set()

    def read(self,path,*,expected=None,kind=None,validator=None,project=None,projection_key=None,index=None,index_key=None,
             readonly=False,canonical_version=None,origin=None):
        origin=origin or caller_origin()
        path=Path(path)
        started=time.monotonic_ns()
        try:data=path.read_bytes()
        except OSError as exc:
            raise ContractError('SOURCE_UNAVAILABLE','$',f'cannot read JSON {path}: {exc}') from exc
        digest=hashlib.sha256(data).hexdigest()
        if expected is not None and digest!=expected:
            raise ContractError('SOURCE_CHANGED','$','verified JSON bytes differ from their current pointer')
        read_hash_ns=time.monotonic_ns()-started
        schema=None
        if kind is not None:
            from . import validation
            schema=hashlib.sha256(Path(validation.__file__).with_name('schemas').joinpath('contracts.json').read_bytes()).hexdigest()
        key=(str(path.resolve()),kind,id(validator),schema)
        with self.lock:
            if schema is not None:
                if self.schema_sha is not None and schema!=self.schema_sha:
                    raise ContractError('SOURCE_CHANGED','$','schema changed during verified JSON scope')
                self.schema_sha=schema
            self.stats['reads']+=1;self.stats['bytes']+=len(data)
            self.stats['read_hash_ns']+=read_hash_ns
            cached=self.entries.get(key)
            if cached is None or cached['digest']!=digest:
                started=time.monotonic_ns()
                try:value=json.loads(data.decode('utf-8'))
                except (UnicodeError,ValueError) as exc:
                    raise ContractError('SOURCE_UNAVAILABLE','$',f'cannot read JSON {path}: {exc}') from exc
                self.stats['parses']+=1
                self.stats['parse_ns']+=time.monotonic_ns()-started
                if kind is not None:
                    started=time.monotonic_ns()
                    validator(kind,value);self.stats['validations']+=1
                    self.stats['validation_ns']+=time.monotonic_ns()-started
                cached={'digest':digest,'value':value,'views':{},'indexes':{},'sizes':{},'documents':{},'validator':validator}
                self.entries[key]=cached
            else:self.stats['hits']+=1
            self.entries.move_to_end(key)
            while len(self.entries)>self.limit:self.entries.popitem(last=False)
            if readonly:
                if project is not None or index is not None:raise ValueError('readonly snapshot requires the complete verified source')
                from .verified_snapshot import SnapshotDocument,VerifiedSnapshot,CANONICAL_RULE_VERSION
                version=CANONICAL_RULE_VERSION if canonical_version is None else canonical_version
                document_key=(key[0],digest,schema,version)
                if document_key not in cached['documents']:
                    started=time.monotonic_ns();cached['documents'][document_key]=SnapshotDocument(cached['value'],self,document_key)
                    self.stats['freeze_ns']+=time.monotonic_ns()-started
                    self.stats['immutable_versions']+=1;self.snapshot_versions.add(document_key)
                self.stats['snapshot_reads']+=1
                self.snapshot_origins[origin]=self.snapshot_origins.get(origin,0)+1
                return VerifiedSnapshot(cached['documents'][document_key],origin)
            source=cached['value']
            if index is not None:
                if index_key is None:raise ValueError('pure index requires an explicit key')
                if index_key not in cached['indexes']:
                    started=time.monotonic_ns();cached['indexes'][index_key]=index(source)
                    self.stats['index_ns']+=time.monotonic_ns()-started;self.stats['index_builds']+=1
                source=cached['indexes'][index_key]
            view_key=('projection',index_key,projection_key) if project is not None else ('full',)
            if project is None:result=source
            elif projection_key is None:
                started=time.monotonic_ns();result=project(source)
                self.stats['projection_ns']+=time.monotonic_ns()-started;self.stats['projections']+=1
            else:
                if view_key not in cached['views']:
                    started=time.monotonic_ns();cached['views'][view_key]=project(source)
                    self.stats['projection_ns']+=time.monotonic_ns()-started;self.stats['projections']+=1
                result=cached['views'][view_key]
            # Count logical JSON value nodes once per immutable view, not bytes
            # serialized on every hit. These are copy-volume metrics, not I/O.
            if projection_key is None and project is not None:volume=None
            else:
                if view_key not in cached['sizes']:cached['sizes'][view_key]=_value_nodes(result)
                volume=cached['sizes'][view_key]
            started=time.monotonic_ns();returned=copy.deepcopy(result)
            self.note_copy(origin,project is None,time.monotonic_ns()-started,volume)
            return returned

    def note_copy(self,origin,full,elapsed,nodes):
        kind='full' if full else 'projected'
        self.stats['copy_ns']+=elapsed;self.stats[kind+'_copies']+=1;self.stats[kind+'_copy_ns']+=elapsed
        if nodes is not None:
            self.stats['copied_value_nodes']+=nodes;self.stats[kind+'_copy_nodes']+=nodes
        row=self.copy_origins.setdefault(kind+':'+origin,{'calls':0,'ns':0,'value_nodes':0})
        row['calls']+=1;row['ns']+=elapsed;row['value_nodes']+=nodes or 0


def caller_origin():
    frame=sys._getframe(2)
    return Path(frame.f_code.co_filename).name+':'+frame.f_code.co_name+':'+str(frame.f_lineno)


def _value_nodes(value):
    pending=[value];count=0
    while pending:
        current=pending.pop();count+=1
        if isinstance(current,Mapping):pending.extend(current.values())
        elif isinstance(current,(list,tuple)):pending.extend(current)
    return count


def workflow_read_scope(function):
    """Bind only pure parsing reuse to one adapter invocation, including SDK threads."""
    @wraps(function)
    def invoke(owner,*args,**kwargs):
        service=getattr(owner,'workflow',owner)
        scope=getattr(service,'read_projection',None)
        with scope() if scope else nullcontext():
            return function(owner,*args,**kwargs)
    return invoke
