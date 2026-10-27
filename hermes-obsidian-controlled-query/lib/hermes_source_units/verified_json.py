"""Operation-local pure JSON/shape reuse, always gated by freshly read bytes."""
import copy
import hashlib
import json
import threading
from collections import OrderedDict
from contextlib import nullcontext
from functools import wraps
from pathlib import Path

from .validation import ContractError


class VerifiedJsonReads:
    def __init__(self, limit=128):
        self.limit=limit;self.entries=OrderedDict();self.lock=threading.RLock()
        self.schema_sha=None
        self.stats={'reads':0,'bytes':0,'parses':0,'validations':0,'hits':0,'projections':0}

    def read(self,path,*,expected=None,kind=None,validator=None,project=None,projection_key=None):
        path=Path(path)
        try:data=path.read_bytes()
        except OSError as exc:
            raise ContractError('SOURCE_UNAVAILABLE','$',f'cannot read JSON {path}: {exc}') from exc
        digest=hashlib.sha256(data).hexdigest()
        if expected is not None and digest!=expected:
            raise ContractError('SOURCE_CHANGED','$','verified JSON bytes differ from their current pointer')
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
            cached=self.entries.get(key)
            if cached is None or cached['digest']!=digest:
                try:value=json.loads(data.decode('utf-8'))
                except (UnicodeError,ValueError) as exc:
                    raise ContractError('SOURCE_UNAVAILABLE','$',f'cannot read JSON {path}: {exc}') from exc
                self.stats['parses']+=1
                if kind is not None:
                    validator(kind,value);self.stats['validations']+=1
                cached={'digest':digest,'value':value,'views':{},'validator':validator}
                self.entries[key]=cached
            else:self.stats['hits']+=1
            self.entries.move_to_end(key)
            while len(self.entries)>self.limit:self.entries.popitem(last=False)
            if project is None:return copy.deepcopy(cached['value'])
            if projection_key is None:return copy.deepcopy(project(cached['value']))
            if projection_key not in cached['views']:
                cached['views'][projection_key]=project(cached['value']);self.stats['projections']+=1
            return copy.deepcopy(cached['views'][projection_key])


def workflow_read_scope(function):
    """Bind only pure parsing reuse to one adapter invocation, including SDK threads."""
    @wraps(function)
    def invoke(owner,*args,**kwargs):
        service=getattr(owner,'workflow',owner)
        scope=getattr(service,'read_projection',None)
        with scope() if scope else nullcontext():
            return function(owner,*args,**kwargs)
    return invoke
