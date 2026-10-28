"""A pure calculation's captured JSON bytes; never a permission or disk cache."""
import copy
import json
from contextlib import contextmanager
from contextvars import ContextVar

_READS = ContextVar('record_snapshot_reads', default=None)


@contextmanager
def record_snapshot(records):
    """Limit reuse to the caller's captured files and this calculation only."""
    token = _READS.set((records, {}))
    try:
        yield
    finally:
        _READS.reset(token)


def snapshot_json(path):
    scope = _READS.get()
    if scope is None or path not in scope[0]:
        return False, None
    records, parsed = scope
    from .read_accounting import note_capture
    note_capture(path)
    data, digest = records[path]
    if digest not in parsed:
        parsed[digest] = json.loads(data.decode('utf-8'))
    # Repository readers may mutate their return values. Never expose our cache.
    return True, copy.deepcopy(parsed[digest])
