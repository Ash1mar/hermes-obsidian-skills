"""Incremental console projections retain scope and explicit authorization."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
        self.assertNotIn('incremental_manual_sources', result)
        self.assertTrue(settings({**body, 'incremental_manual_sources': True},
                                 {'provider_config': '/configured/provider.json'}, ValueError)['incremental_manual_sources'])
        with self.assertRaises(ValueError):
            settings({**body, 'incremental_manual_sources': 'true'}, {}, ValueError)

    def test_manual_source_actions_bind_exact_path_hash_and_keep_pause_separate(self):
        from incremental import fingerprint
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            directory = base / 'console-fixture'
            directory.mkdir()
            request = {'operation_id': directory.name, 'actor': 'operator', 'vault_name': 'Fixture',
                       'vault_path': str(base / 'vault'), 'workflow_mode': 'incremental_v1',
                       'sources': ['/first.pdf', '/second.pdf'], 'retrieval_policy': {'query_enabled': False}}
            root = base / 'vault/_system/ledgers/incremental-ingest' / directory.name
            (root / 'control').mkdir(parents=True)
            run = {'state': 'active', 'revision': 5, 'sources': {'10_Raw/first.pdf': {'state': 'pending'},
                   '10_Raw/second.pdf': {'state': 'pending'}}, 'round_ids': [], 'map_result_ids': []}
            control = {'state': 'paused', 'revision': 4, 'boundary': {'stage': 'prepare',
                       'scope': {'scope_kind': 'run', 'scope_id': directory.name}}, 'active_scopes': {},
                       'manual_source_admission': {'10_Raw/first.pdf': {'state': 'held', 'sha256': 'a' * 64},
                                                   '10_Raw/second.pdf': {'state': 'held', 'sha256': 'b' * 64}}}
            (directory / 'request.json').write_text(json.dumps(request))
            (root / 'run.json').write_text(json.dumps(run))
            (root / 'control/head.json').write_text(json.dumps(control))
            latest = {'id': 'attempt-one', 'returncode': 0}
            projected = progress(directory, request, [latest], False, False, True)
            sources = projected['incremental']['sources']
            first, second = [row['actions'][0] for row in sources]
            self.assertNotEqual(first['node'], second['node'])
            self.assertEqual(projected['nodes'][0]['actions'][0]['id'], 'incremental-continue')
            self.assertEqual(first['source_path'], '10_Raw/first.pdf')
            self.assertIn('继续后开始', first['label'])
            script = base / 'skills/hermes-obsidian-governed-ingest-orchestrator/scripts/manage_incremental_ingest.py'
            script.parent.mkdir(parents=True)
            script.write_text('# fixture only')
            emitted = []
            attempt = {**second, 'id': 'release-second', 'action': second['id']}
            argv = command(directory, attempt, {'skills_root': str(base / 'skills')}, lambda p, value: emitted.append(value))
            self.assertEqual(argv[-3], 'control')
            self.assertEqual(emitted[0]['action'], 'release-source')
            self.assertEqual(emitted[0]['source_path'], '10_Raw/second.pdf')
            self.assertEqual(emitted[0]['source_sha256'], 'b' * 64)
            self.assertNotIn('boundary_fingerprint', emitted[0])
            with self.assertRaises(ValueError):
                command(directory, {**attempt, 'source_sha256': 'a' * 64},
                        {'skills_root': str(base / 'skills')}, lambda *_: None)

    def test_http_source_action_cannot_substitute_another_source(self):
        import importlib.util
        from types import SimpleNamespace
        path = Path(__file__).with_name('server.py')
        spec = importlib.util.spec_from_file_location('manual_source_console', path)
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            directory = base / 'console-fixture'
            directory.mkdir()
            action = {'id': 'incremental-release-source', 'node': 'source-a', 'attempt': 'prior',
                      'label': '开始处理此文件', 'control_revision': 3, 'run_revision': 5,
                      'source_path': '10_Raw/a.pdf', 'source_sha256': 'a' * 64}
            console = object.__new__(server.Console)
            console.state, console.config = base, {'writable': True}
            console.gateway = SimpleNamespace(status={'ready': True})
            console.lock = lambda: (base / 'lease').open('a')
            console.find = lambda _: (directory, False)
            payload = {**action, 'action': action['id']}
            with patch.object(server, 'progress', return_value={'nodes': [{'actions': [action]}]}), \
                    patch.object(server.subprocess, 'Popen') as popen:
                with self.assertRaises(server.UserError):
                    console.launch({**payload, 'source_path': '10_Raw/b.pdf'}, directory.name)
                popen.assert_not_called()
                result = console.launch(payload, directory.name)
                record = json.loads((directory / 'attempts' / result['attempt'] / 'attempt.json').read_text())
                self.assertEqual((record['source_path'], record['source_sha256']),
                                 (action['source_path'], action['source_sha256']))

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
