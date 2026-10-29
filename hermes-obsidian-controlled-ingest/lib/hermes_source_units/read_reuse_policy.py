"""Conservative reference modes for read-only comparisons; never grants."""
from contextlib import contextmanager
from contextvars import ContextVar

_POLICY=ContextVar('read_reuse_policy',default=(True,True,True))


def hash_reuse():return _POLICY.get()[0]
def schema_reuse():return _POLICY.get()[1]
def query_sharing():return _POLICY.get()[2]


@contextmanager
def reference_reads(*,hashes=False,schema=False,queries=False):
    """Only select more conservative reference paths; no authority is cached.

    Used by isolated diagnostics to compare fixed material in one process.
    There is no environment/CLI switch enabling weaker execution checks.
    """
    token=_POLICY.set((hashes,schema,queries))
    try:yield
    finally:_POLICY.reset(token)
