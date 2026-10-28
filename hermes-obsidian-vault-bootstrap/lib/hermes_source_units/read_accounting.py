"""Invocation-local logical byte accounting; never a read cache or authority."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

_LEDGER=ContextVar('logical_read_ledger',default=None)


class ReadLedger:
    def __init__(self):
        self.rows={};self.boundaries={};self.seen_json={};self.lock=threading.Lock()

    def note(self,path,size,origin,*,captured=False,failed=False,count=1):
        from .runtime_telemetry import _TRACE
        trace=_TRACE.get();boundary=trace.get('read_boundary_id',trace.get('span_id','unbound'))
        name=str(path);kind=trace.get('read_boundary_name',trace.get('name','unbound'))
        key=(origin,name,kind);now=time.monotonic_ns()
        with self.lock:
            row=self.rows.get(key)
            if row is None:
                row={'origin':origin,'path':name,'boundary':kind,
                    'first_monotonic_ns':now,'last_monotonic_ns':now,'reads':0,'bytes':0,'captures':0,'failures':0}
                self.rows[key]=row
            row['last_monotonic_ns']=now
            row['captures' if captured else 'reads']+=count
            if not captured:row['bytes']+=size
            row['failures']+=int(failed)
            # Time buckets also split broad unbound spans for recovery/window/
            # drain accounting, without retaining one row per individual read.
            bkey=(boundary,origin,now//1_000_000_000);batch=self.boundaries.get(bkey)
            if batch is None:
                batch={'boundary_id':boundary,'boundary':kind,'origin':origin,
                    'first_monotonic_ns':now,'last_monotonic_ns':now,'reads':0,'bytes':0,'captures':0,'failures':0,
                    'json_paths':{},'duplicate_json_reads':0}
                self.boundaries[bkey]=batch
            batch['last_monotonic_ns']=now;batch['captures' if captured else 'reads']+=count
            if not captured:batch['bytes']+=size
            batch['failures']+=int(failed)
            if not captured and (origin.startswith('verified_json:') or origin=='source_units._load_json'):
                seen=self.seen_json.setdefault((boundary,origin),set());prior=name in seen;seen.add(name)
                batch['json_paths'][name]=batch['json_paths'].get(name,0)+1
                batch['duplicate_json_reads']+=int(prior>0)


@contextmanager
def accounting_scope():
    existing=_LEDGER.get()
    if existing is not None:yield existing;return
    ledger=ReadLedger();token=_LEDGER.set(ledger)
    try:yield ledger
    finally:_LEDGER.reset(token)


def note_capture(path):
    ledger=_LEDGER.get()
    if ledger is not None:ledger.note(path,0,'record_snapshot',captured=True)


def read_bytes(path,origin):
    from .evidence_snapshot import current
    session=current()
    if session is not None:
        data=session.read(path,origin)
        if data is not None:return data
    return _raw_read_bytes(path,origin)


def _raw_read_bytes(path,origin):
    ledger=_LEDGER.get()
    try:data=path.read_bytes()
    except OSError:
        if ledger is not None:ledger.note(path,0,origin,failed=True)
        raise
    if ledger is not None:ledger.note(path,len(data),origin)
    return data
