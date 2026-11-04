"""User cancellation supersedes automatic continuation, including after restart."""
from contextlib import contextmanager
from contextvars import ContextVar
from .source_units import _load_json, _write_atomic, _json_bytes
from .validation import ContractError

_AUTOMATIC = ContextVar('automatic_workflow_recovery', default=None)


def stop_path(vault, wid):
    return vault/f'_system/ledgers/ingest-workflows/{wid}/automatic-control/operator-stop.json'


def epoch(vault, wid):
    path=stop_path(vault,wid)
    return _load_json(path)['input_digest'] if path.exists() else None


@contextmanager
def automatic(vault, wid, expected_epoch):
    token=_AUTOMATIC.set((str(vault),wid,expected_epoch))
    try:
        check(vault,wid)
        yield
    finally:_AUTOMATIC.reset(token)


def check(vault, wid):
    context=_AUTOMATIC.get()
    if context is not None and (context[:2]!=(str(vault),wid) or context[2]!=epoch(vault,wid)):
        raise ContractError('WORKFLOW_STOPPED','$','operator cancellation superseded automatic continuation')


def record_cancel(vault, request):
    """Called under the workflow lock, after request authentication."""
    check(vault,request['workflow_id'])
    if _AUTOMATIC.get() is None:
        _write_atomic(stop_path(vault,request['workflow_id']),_json_bytes({
            'input_digest':request['input_digest'],'actor':request['actor'],
            'expected_revision':request['expected_revision']}))
