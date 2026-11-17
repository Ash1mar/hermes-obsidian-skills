"""Explicit operator checkpoints for the independent short-path launcher."""

from contextlib import contextmanager
import re
import time

from hermes_source_units.source_units import _json_bytes, _load_json, _write_atomic
from short_evidence_pass import require


NODES = ('bootstrap', 'prepare', 'exact', 'topics', 'mapping', 'coordination',
         'writing', 'formal-build', 'release', 'acceptance')
PAUSED = 0
DETACHED = 75


class OperatorControl:
    def __init__(self, root, operation_id, attempt_id, pause_before, continuing, attaching):
        require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', attempt_id), 'invalid operator attempt ID')
        require(pause_before is None or pause_before in NODES, 'unknown operator pause boundary')
        self.root = root
        self.path = root / 'operator-state.json'
        self.operation_id = operation_id
        self.attempt_id = attempt_id
        self.pause_before = pause_before
        self.resume = None
        self.current = None
        with self.lock():
            prior = _load_json(self.path) if self.path.is_file() else {}
            require(not prior or (prior['operation_id'] == operation_id and prior['attempt_id'] != attempt_id),
                    'operator attempt identity changed or already used')
            if continuing:
                require(prior.get('state') == 'paused', 'continue requires an explicitly paused checkpoint')
                require(pause_before is None or NODES.index(pause_before) > NODES.index(prior['node']),
                        'next pause must follow the current checkpoint')
                self.resume = prior
            elif attaching:
                require(prior.get('state') not in ('paused', 'completed')
                        and prior.get('node') in (None, 'bootstrap', 'prepare', 'exact'),
                        'source attach cannot release a pause or resume downstream execution')
                require(pause_before is None or NODES.index(pause_before) >= NODES.index('exact'),
                        'source attach can pause at exact handoff or a later checkpoint')
                mailbox = root / 'operator-pause.json'
                pending = _load_json(mailbox) if mailbox.is_file() else {}
                saved_target = (pending.get('node') if prior and pending.get('attempt_id') == prior['attempt_id']
                                else prior.get('pending_pause'))
                require(saved_target is None or pause_before in (None, saved_target),
                        'source attach cannot replace a pending pause')
                self.pause_before = saved_target or pause_before
            else:
                require(not prior, 'operator state already exists')

    @contextmanager
    def lock(self):
        import fcntl

        with (self.root / 'operator.lock').open('a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _write(self, node, state, evidence, **details):
        record = {'contract': 'short-operator-state/v1', 'operation_id': self.operation_id,
                  'attempt_id': self.attempt_id, 'node': node, 'state': state,
                  'completed': list(NODES if state == 'completed' else NODES[:NODES.index(node)]),
                  'evidence': evidence, 'updated_at': time.time(), 'pending_pause': self.pause_before, **details}
        _write_atomic(self.path, _json_bytes(record))
        self.current = record

    def checkpoint(self, node, evidence):
        """Admit work at a boundary, or commit a pause before that work starts."""
        with self.lock():
            if self.resume:
                paused_node = self.resume['node']
                if NODES.index(node) < NODES.index(paused_node):
                    return False
                require(node == paused_node and evidence == self.resume['evidence'],
                        'paused checkpoint identity or evidence changed')
                self.resume = None
            if self.current and self.current['node'] == node:
                return self.current['state'] == 'paused'
            require(not self.current or NODES.index(self.current['node']) < NODES.index(node),
                    'operator checkpoint moved backwards')
            mailbox = self.root / 'operator-pause.json'
            pause = _load_json(mailbox) if mailbox.is_file() else {}
            if pause.get('attempt_id') == self.attempt_id:
                self.pause_before = pause.get('node')
            requested = self.pause_before
            require(requested is None or requested in NODES, 'unknown operator pause boundary')
            # Short planning may omit nodes with no work. Stop at the next valid boundary.
            due = requested is not None and NODES.index(requested) <= NODES.index(node)
            self._write(node, 'paused' if due else 'running', evidence, **({'requested_node': requested} if due else {}))
            if due:
                print(f'Operator paused before {node}', flush=True)
            return due

    def finish(self, state, **details):
        # An invalid continuation must leave the original pause intact.
        if self.resume:
            return
        with self.lock():
            prior = self.current or {'node': 'prepare', 'evidence': {}}
            self._write(prior['node'], state, prior['evidence'], **details)
