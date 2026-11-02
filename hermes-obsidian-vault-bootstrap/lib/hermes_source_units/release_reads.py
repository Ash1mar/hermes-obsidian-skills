"""One release calculation's byte/JSON reuse, with fresh precommit hashes.

Never includes workflow authority, leases or permissions. No cross-operation
cache; independent validation starts a new session. Writers remain serialized
by the existing Finalize/native workflow guards. Out-of-band maintenance must
stop consumers; a final byte check detects changes during this calculation.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import json
import time

from .validation import ContractError
from .runtime_telemetry import span

_CURRENT = ContextVar('release_operation_reads', default=None)


class ReleaseReads:
    def __init__(self, vault):
        self.vault = vault
        self.records = {}
        self.parsed = {}
        self.proofs = {}
        self.stats = dict(reads=0, bytes=0, reuse_hits=0, json_parses=0,
                          json_reuses=0, confirmation_reads=0, confirmation_bytes=0,
                          read_ns=0, hash_ns=0, parse_ns=0)

    def accepts(self, path):
        return path.is_relative_to(self.vault) and not path.is_relative_to(self.vault/'_system/ledgers')

    def read(self, path):
        if path in self.records:
            self.stats['reuse_hits'] += 1
            return self.records[path][0]
        from .read_accounting import _raw_read_bytes
        started = time.monotonic_ns()
        data = _raw_read_bytes(path, 'release_operation')
        self.stats['read_ns'] += time.monotonic_ns()-started
        started = time.monotonic_ns()
        digest = hashlib.sha256(data).digest()
        self.stats['hash_ns'] += time.monotonic_ns()-started
        self.records[path] = data, digest
        self.stats['reads'] += 1
        self.stats['bytes'] += len(data)
        return data

    def load(self, path):
        data = self.read(path)
        if path not in self.parsed:
            started = time.monotonic_ns()
            self.parsed[path] = json.loads(data.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n'))
            self.stats['parse_ns'] += time.monotonic_ns()-started
            self.stats['json_parses'] += 1
        else:
            self.stats['json_reuses'] += 1
        return copy.deepcopy(self.parsed[path])

    def confirm(self):
        from .read_accounting import _raw_read_bytes
        with span('release_input_confirmation', vault=self.vault):
            for path, (_, expected) in self.records.items():
                data = _raw_read_bytes(path, 'release_input_confirmation')
                self.stats['confirmation_reads'] += 1
                self.stats['confirmation_bytes'] += len(data)
                if hashlib.sha256(data).digest() != expected:
                    raise ContractError('SOURCE_CHANGED', '$', 'release input changed: '+str(path))


@contextmanager
def operation(vault, name):
    existing = _CURRENT.get()
    if existing is not None:
        yield existing
        return
    reads = ReleaseReads(vault)
    token = _CURRENT.set(reads)
    try:
        with span(name, vault=vault) as trace:
            try:
                yield reads
            finally:
                trace.update(reads.stats, unique_files=len(reads.records))
    finally:
        _CURRENT.reset(token)


def current(path):
    reads = _CURRENT.get()
    return reads if reads is not None and reads.accepts(path) else None


def body(path):
    from .read_accounting import read_bytes
    return read_bytes(path, 'release_body')
