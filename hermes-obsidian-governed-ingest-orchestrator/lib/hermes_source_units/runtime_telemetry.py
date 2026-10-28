"""Best-effort host timelines and wake hints, never workflow authority."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import uuid

_TRACE = ContextVar('ingest_runtime_trace', default={})
try:
    _CLOCK_DOMAIN = 'linux-boot:'+Path('/proc/sys/kernel/random/boot_id').read_text().strip()
except OSError:
    _CLOCK_DOMAIN = f'process:{os.getpid()}:{uuid.uuid4().hex}'


def clock_sample():
    """Paired host clocks; wall timestamps identify events, monotonic measures time."""
    return {'wall_time_ns':time.time_ns(),'monotonic_ns':time.monotonic_ns(),
            'clock_domain':_CLOCK_DOMAIN}


def monotonic_deadline(start, seconds):
    """An observer deadline never extends because the wall clock moves."""
    if type(seconds) is not int or seconds<=0:
        raise ValueError('observation seconds must be a positive integer')
    return {**start,'deadline_monotonic_ns':start['monotonic_ns']+seconds*1_000_000_000,
            'seconds':seconds}


def remaining_ns(deadline, current=None):
    current=clock_sample() if current is None else current
    if current['clock_domain']!=deadline['clock_domain']:
        raise ValueError('observation clock domain changed; stop rather than reset deadline')
    return max(0,deadline['deadline_monotonic_ns']-current['monotonic_ns'])


def runtime_directory(vault, workflow_id):
    root = (Path.home()/'.cache/hermes-skill-runtime/timelines' if os.name == 'posix'
            else Path(tempfile.gettempdir())/'hermes-skill-runtime/timelines')
    key = hashlib.sha256((str(Path(vault).resolve())+'\0'+workflow_id).encode()).hexdigest()[:24]
    return root/key


@contextmanager
def span(name, *, vault=None, workflow_id=None, binding=None, **fields):
    parent = _TRACE.get()
    binding = binding or {}
    identity = {**parent, **{k:binding[k] for k in ('workflow_id','task_id','node') if k in binding}}
    identity.update(vault=str(vault or identity.get('vault') or os.environ.get('TERMINAL_CWD','')),
        workflow_id=workflow_id or identity.get('workflow_id') or os.environ.get('HERMES_INGEST_WORKFLOW_ID'),
        task_id=identity.get('task_id') or os.environ.get('HERMES_KANBAN_TASK'),
        node=identity.get('node') or os.environ.get('HERMES_INGEST_NODE'),
        native_run_id=os.environ.get('HERMES_KANBAN_RUN_ID'))
    if identity.get('node','') and identity['node'].startswith('pass-slice:'):
        identity['slice_id']=identity['node'].partition(':')[2]
    started = clock_sample()
    trace = {**identity, 'span_id':uuid.uuid4().hex, 'parent_span_id':parent.get('span_id'),
             'name':name, 'started_at':started['wall_time_ns']/1_000_000_000,
             'started_monotonic_ns':started['monotonic_ns'],
             'clock_domain':started['clock_domain'], 'pid':os.getpid(), **fields}
    if name in ('worker_authority_check','worker_live_control_check','native_output_authentication'):
        trace.update(read_boundary_id=trace['span_id'],read_boundary_name=name)
    else:
        for key in ('read_boundary_id','read_boundary_name'):
            if key in parent:trace[key]=parent[key]
    token = _TRACE.set(trace)
    error = None
    try:
        yield trace
    except BaseException as exc:
        error = getattr(exc,'code',type(exc).__name__)
        raise
    finally:
        _TRACE.reset(token)
        try:
            if trace['workflow_id'] and trace['vault']:
                finished = clock_sample()
                row = {**trace, 'finished_at':finished['wall_time_ns']/1_000_000_000,
                       'finished_monotonic_ns':finished['monotonic_ns'],
                       'elapsed_ms':round((finished['monotonic_ns']-started['monotonic_ns'])/1_000_000,3), 'error':error}
                root = runtime_directory(trace['vault'],trace['workflow_id'])
                root.mkdir(parents=True,exist_ok=True)
                with (root/f'{os.getpid()}.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(row,separators=(',',':'))+'\n')
        except (OSError, ValueError, TypeError):
            pass


def measured(name, identity=None):
    def decorate(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            with span(name, **(identity(*args, **kwargs) if identity else {})):
                return function(*args, **kwargs)
        return invoke
    return decorate


def notify_reconciler(vault, workflow_id):
    """Wake only the exact local watcher process; hint does not grant execution."""
    if os.name != 'posix':
        return False
    try:
        marker = json.loads((runtime_directory(vault,workflow_id)/'watcher.json').read_text())
        proc = Path('/proc')/str(marker['pid'])
        args = [s.decode() for s in (proc/'cmdline').read_bytes().split(b'\0') if s]
        if (proc.stat().st_uid != os.getuid() or marker['workflow_id'] != workflow_id
                or not any(Path(a).name == 'watch_ingest_workflow.py' for a in args)
                or '--workflow-id' not in args or args[args.index('--workflow-id')+1] != workflow_id
                or '--vault' not in args or Path(args[args.index('--vault')+1]).resolve() != Path(vault).resolve()):
            return False
        os.kill(marker['pid'],signal.SIGUSR1)
        return True
    except (OSError,ValueError,KeyError,IndexError,UnicodeError):
        return False
