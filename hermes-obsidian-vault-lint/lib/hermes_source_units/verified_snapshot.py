"""Immutable JSON snapshots and pure canonical values, never live authority."""
from collections.abc import Mapping
from types import MappingProxyType
import time
from .validation import fingerprint

CANONICAL_RULE_VERSION='hermes-canonical-json/v1'


def field_projection(value,name,keys,default=None):
    """Copy only required subfields of immutable data at the original access."""
    if not isinstance(value,VerifiedSnapshot):return value.get(name,default)
    def project(record):
        field=record.get(name,default)
        return {k:field[k] for k in keys if k in field} if isinstance(field,Mapping) else field
    return value.select(('field_projection',name,tuple(keys),repr(default)),project)


def freeze(value):
    if isinstance(value,MappingProxyType):return value
    if isinstance(value,dict):return MappingProxyType({k:freeze(v) for k,v in value.items()})
    if isinstance(value,list):return tuple(freeze(v) for v in value)
    return value


def thaw(value):
    if isinstance(value,Mapping):return {k:thaw(v) for k,v in value.items()}
    if isinstance(value,tuple):return [thaw(v) for v in value]
    return value


class SnapshotDocument:
    """All data handed to selectors is recursively immutable."""
    def __init__(self,value,cache,key):
        self.data=freeze(value);self.cache=cache;self.key=key
        self.views={};self.sizes={};self.canonical=None

    def fingerprint(self,origin):
        with self.cache.lock:
            self.cache.stats['canonical_requests']+=1
            if self.canonical is not None:
                self.cache.stats['canonical_hits']+=1
                return self.canonical,False,0
            # Call the unchanged canonical function on a detached ordinary JSON
            # value. This occurs lazily at the original fingerprint boundary.
            value=self.copy(self.data,origin+':canonical',full=True,key=('full',))
            started=time.monotonic_ns();result=fingerprint(value);elapsed=time.monotonic_ns()-started
            self.canonical=result
            self.cache.stats['canonical_builds']+=1;self.cache.stats['canonical_ns']+=elapsed
            return result,True,elapsed

    def copy(self,value,origin,*,full=False,key=None):
        from .verified_json import _value_nodes
        with self.cache.lock:
            if key not in self.sizes:self.sizes[key]=_value_nodes(value)
            started=time.monotonic_ns();result=thaw(value);elapsed=time.monotonic_ns()-started
            self.cache.note_copy(origin,full,elapsed,self.sizes[key])
            return result


class VerifiedSnapshot(Mapping):
    """Read-only handle; ordinary field results are always detached JSON copies."""
    __slots__=('__document','__origin')
    def __init__(self,document,origin):
        object.__setattr__(self,'_VerifiedSnapshot__document',document)
        object.__setattr__(self,'_VerifiedSnapshot__origin',origin)
    def __setattr__(self,*_):raise TypeError('verified snapshot is immutable')
    def __getitem__(self,key):
        return self.__document.copy(self.__document.data[key],self.__origin+'['+key+']',key=('field',key))
    def __iter__(self):return iter(self.__document.data)
    def __len__(self):return len(self.__document.data)
    def __contains__(self,key):return key in self.__document.data
    def __deepcopy__(self,memo):
        result=self.materialize();memo[id(self)]=result;return result
    def materialize(self):
        return self.__document.copy(self.__document.data,self.__origin+':materialize',full=True,key=('full',))
    def canonical_result(self):return self.__document.fingerprint(self.__origin)
    @property
    def canonical_fingerprint(self):return self.canonical_result()[0]
    @property
    def content_key(self):return self.__document.key
    def select(self,key,project):
        """Lazy pure selector at its original check position; no verdict caching."""
        document=self.__document
        with document.cache.lock:
            if key not in document.views:document.views[key]=freeze(project(document.data))
            return document.copy(document.views[key],self.__origin+':select:'+str(key[0]),key=('select',key))
