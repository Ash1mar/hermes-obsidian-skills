"""Bound incremental qualification's pre-model lock acquisition and retry evidence."""
from contextlib import ExitStack, contextmanager
import time

from .source_units import _exclusive_lock
from .validation import ContractError, fingerprint


WAIT_CONTRACT = 'incremental-source-qualification-wait/v1'
LOCK_WAIT_SECONDS = 5
BACKOFF_SECONDS = (2, 4)
LOCK_CONTENTION_CODES = frozenset(('LOCK_BUSY', 'LOCK_TIMEOUT'))


class QualificationContention(ContractError):
    def __init__(self, code, lock, wait_ms):
        super().__init__(code, '$', f'Incremental source qualification could not acquire {lock}')
        self.lock = lock
        self.wait_ms = wait_ms


def qualification_id(run_id, snapshot_id):
    return 'incremental-source-' + fingerprint([run_id, snapshot_id])[:48]


def check_qualification_active(control):
    if control.status()['state'] != 'active':
        raise ContractError('INCREMENTAL_PAUSED', '$', 'new source qualification or Map dispatch is held')


@contextmanager
def qualification_lock(lock, deadline, check):
    """Only lock acquisition can classify an error as retryable contention."""
    with ExitStack() as stack:
        check()
        started = time.monotonic()
        try:
            stack.enter_context(_exclusive_lock(lock, timeout=max(0, deadline - started), check=check))
        except ContractError as error:
            if error.code not in LOCK_CONTENTION_CODES:
                raise
            raise QualificationContention(error.code, lock, round((time.monotonic() - started) * 1000)) from error
        check()
        yield
