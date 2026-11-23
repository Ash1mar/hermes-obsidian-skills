"""An interrupted round recovers its exact unsent native requests."""

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator'
sys.path[:0] = [str(SKILL / 'lib'), str(SKILL / 'scripts')]

import incremental_network
import model_transport
import run_short_evidence_test as native
from hermes_source_units.incremental_sources import pinned


class NetworkRoundTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.vault = Path(directory.name)
        self.root = self.vault / 'native' / 'writing'
        self.head = {}
        self.domain = SimpleNamespace(vault=self.vault, root=lambda _: self.vault,
                                      status=lambda _: {'reservations': {}},
                                      round_input=lambda *_: {'input_fingerprint': 'frozen'})
        self.operation = {'incremental': {'scope_id': 'round-1', 'stage': 'writing'},
                          'page_capacity': 10, 'pass_capacity': 10}
        self.ids = ['completed', 'unknown', 'probe', 'later']
        self.rows = [{'id': value, 'phase': 'incremental-page-write'} for value in self.ids]
        self.states = dict.fromkeys(self.ids, 'unsent')
        self.circuit = None
        self.invocations = []
        self.fail_probe = False
        self.settled = []
        (self.root / 'jobs').mkdir(parents=True)
        mocks = (
            patch.object(incremental_network, 'IncrementalControl', return_value=SimpleNamespace(
                status=lambda: self.head, vault=self.vault)),
            patch.object(model_transport, 'circuit_status', side_effect=lambda _: self.circuit),
            patch.object(native, 'network_scope', return_value='scope'),
            patch.object(native, 'network_history', side_effect=self.history),
            patch.object(native, 'stage', side_effect=self.stage),
        )
        for mock in mocks:
            mock.start()
            self.addCleanup(mock.stop)

    def history(self, _root, _operation, rows, _reservations):
        result = {}
        for row in rows:
            request = row['id']
            entry = {'state': self.states[request]}
            path = self.root / 'jobs' / (request + '.result.json')
            if path.is_file():
                entry['proof'] = {'result': pinned(self.vault, path.relative_to(self.vault).as_posix())}
            result[request] = entry
        return result

    def acknowledge(self, request):
        self.states[request] = 'completed'
        (self.root / 'jobs' / (request + '.result.json')).write_text('{"state":"settled"}', encoding='utf-8')

    def stage(self, _root, _operation, rows, capacity, *, network_probe=None):
        ids = [row['id'] for row in rows]
        self.invocations.append((ids, capacity, network_probe))
        if len(self.invocations) == 1:
            self.acknowledge('completed')
            self.states['unknown'] = 'unconfirmed'
            self.circuit = {'route_id': 'route', 'state': 'open', 'trip_id': 'trip-1'}
            raise native.NetworkStagePaused(self.root, self.circuit)
        if network_probe is not None:
            if self.fail_probe:
                self.states[ids[0]] = 'unconfirmed'
                self.circuit = {'route_id': 'route', 'state': 'open', 'trip_id': 'trip-2'}
                raise native.NetworkStagePaused(self.root, self.circuit)
            self.circuit = {'route_id': 'route', 'state': 'closed', 'trip_id': None}
        elif self.circuit['state'] == 'open':
            raise native.NetworkStagePaused(self.root, self.circuit)
        for request in ids:
            self.acknowledge(request)

    def execute(self):
        return incremental_network.execute(self.domain, 'run', self.root, self.operation, self.rows,
                                           lambda *_: self.settled.append(True))

    def pause(self):
        with self.assertRaises(incremental_network.RoundNetworkPaused) as raised:
            self.execute()
        return raised.exception

    def authorize(self, paused, operation_id):
        self.head['network_continuation'] = {'round_id': 'round-1', 'operation_id': operation_id,
                                             'boundary': {'evidence': paused.evidence}}

    def test_one_original_probe_then_remaining_work_and_idempotent_reentry(self):
        paused = self.pause()
        self.assertEqual(paused.scope, {'scope_kind': 'round', 'scope_id': 'round-1'})
        self.assertEqual(paused.evidence['round_input_fingerprint'], 'frozen')
        self.authorize(paused, 'continue-1')
        result = self.execute()
        self.assertEqual([row['id'] for row, _, _ in result], ['completed', 'probe', 'later'])
        self.assertEqual(self.invocations[1], (['probe'], 1, {
            'route_id': 'route', 'trip_id': 'trip-1', 'operation_id': 'continue-1', 'request_id': 'probe'}))
        self.assertEqual(self.invocations[2], (['later'], 10, None))
        self.assertEqual(self.execute(), result)
        self.assertEqual(len(self.invocations), 3)

    def test_failed_probe_stays_isolated_and_new_authorization_advances_untried_request(self):
        self.authorize(self.pause(), 'continue-1')
        self.fail_probe = True
        second = self.pause()
        self.assertEqual(second.evidence['circuits'][0]['trip_id'], 'trip-2')
        self.pause()
        self.assertEqual(self.invocations[-1][2], None)
        self.authorize(second, 'continue-2')
        self.fail_probe = False
        result = self.execute()
        self.assertEqual(self.invocations[-2][2]['request_id'], 'later')
        self.assertEqual([row['id'] for row, _, _ in result], ['completed', 'later'])


if __name__ == '__main__':
    unittest.main()
