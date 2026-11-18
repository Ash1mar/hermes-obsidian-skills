"""Incremental console projections retain scope and explicit authorization."""
import json
from pathlib import Path
import tempfile
import unittest

from incremental import command, progress, settings


class IncrementalConsoleTests(unittest.TestCase):
    def test_explicit_mode_requires_real_budget_and_preserves_legacy_default(self):
        self.assertEqual(settings({}, {}, ValueError), {})
        with self.assertRaises(ValueError):
            settings({'workflow_mode': 'incremental_v1'}, {}, ValueError)
        budget = {'max_calls': 30, 'max_input_tokens': 100000, 'max_output_tokens': 50000,
                  'phase_limits': {'mapping': 20, 'coordination': 5, 'writing': 20}}
        body = {'workflow_mode': 'incremental_v1', 'incremental_budget': budget,
                'incremental_policy': {'max_maps_per_round': 1}, 'incremental_pause_after': ['mapping', 'writing']}
        result = settings(body, {'provider_config': '/configured/provider.json'}, ValueError)
        self.assertEqual(result['incremental_budget'], budget)
        self.assertEqual(result['incremental_pause_after'], ['mapping', 'writing'])

    def test_paused_scope_does_not_appear_as_a_completed_linear_pipeline(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            directory = base / 'console-fixture'
            directory.mkdir()
            request = {'operation_id': directory.name, 'actor': 'operator', 'vault_name': 'Fixture',
                       'vault_path': str(base / 'vault'), 'workflow_mode': 'incremental_v1',
                       'sources': ['/first.pdf', '/second.pdf'], 'retrieval_policy': {'query_enabled': False}}
            root = base / 'vault/_system/ledgers/incremental-ingest' / directory.name
            (root / 'control').mkdir(parents=True)
            run = {'state': 'active', 'revision': 5, 'sources': {'10_Raw/first.pdf': {'state': 'mapped'},
                   '10_Raw/second.pdf': {'state': 'pending'}}, 'round_ids': [], 'map_result_ids': ['map-one']}
            control = {'state': 'paused', 'revision': 4, 'boundary': {'stage': 'mapping',
                       'scope': {'scope_kind': 'source', 'scope_id': 'snapshot-first'}}, 'active_scopes': {}}
            (root / 'run.json').write_text(json.dumps(run))
            (root / 'control/head.json').write_text(json.dumps(control))
            attempt = {'id': 'attempt-one', 'returncode': 0}
            projected = progress(directory, request, [attempt], False, False, True)
            self.assertEqual(projected['state'], 'paused')
            self.assertEqual(projected['nodes'][0]['actions'][0]['id'], 'incremental-continue')
            self.assertEqual(projected['incremental']['source_counts'], {'mapped': 1, 'pending': 1})
            self.assertEqual(len(projected['nodes']), 1)
            self.assertIsNone(projected['release_id'])


if __name__ == '__main__':
    unittest.main()
