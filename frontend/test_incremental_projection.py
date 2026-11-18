"""Incremental UI keeps native source progress and shared round scope explicit."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from incremental import fingerprint
from incremental_projection import overall_stages, retry_capability, round_projection, source_pipeline


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


class PipelineProjectionTests(unittest.TestCase):
    def test_native_running_source_does_not_wait_for_other_sources_or_mark_whole_flow_done(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'vault/_system/ledgers/incremental-ingest/run'
            root.mkdir(parents=True)
            run = {'run_id': 'run', 'state': 'active', 'source_manifest': [
                {'path': 'a.pdf', 'sha256': 'a' * 64}, {'path': 'b.pdf', 'sha256': 'b' * 64}],
                'map_result_ids': ['map-a'], 'consumed_map_ids': ['map-a'], 'active_round_id': 'round-1'}
            control = {'state': 'active', 'active_scopes': {}}
            node = 'source-prepare:' + fingerprint({'path': 'b.pdf', 'content_sha256': 'b' * 64})[:16]
            workflow = {'kanban': {'board_id': 'sources', 'task_map': [{'node': node, 'task_id': 'task-b'}]}, 'source_outcomes': []}
            reader = lambda board: {'task-b': {'status': 'running', 'claimed': True}} if board == 'sources' else {}
            put(root / 'rounds/round-1/input.json', {'snapshots': ['snap-a'], 'map_result_ids': ['map-a']})
            put(root / 'rounds/round-1/page-changes.json', [{'page_id': 'page-a', 'coverage_scope': {'snapshots': ['snap-a']}}])
            round_ = round_projection(root, 'round-1', run, control, native_reader=reader)
            a = source_pipeline({'path': 'a.pdf', 'state': 'mapped', 'snapshot_id': 'snap-a', 'map_result_id': 'map-a'},
                                root, run, control, workflow, [round_], native_reader=reader)
            b = source_pipeline({'path': 'b.pdf', 'state': 'pending'}, root, run, control, workflow, [round_], native_reader=reader)
            self.assertEqual(b['display_state'], 'running')
            self.assertEqual(b['pipeline'][0]['state'], 'running')
            self.assertEqual(b['related_rounds'], [])
            self.assertEqual(a['related_rounds'][0]['dependent_page_ids'], ['page-a'])
            self.assertEqual(a['related_rounds'][0]['role'], 'input')
            stages = overall_stages([a, b], [round_], run, control)
            self.assertEqual(len(stages), 8)
            self.assertEqual(stages[0]['state'], 'running')
            self.assertEqual(stages[2]['state'], 'active')
            self.assertFalse(b['retry']['allowed'])

    def test_retry_requires_exact_blocked_unconsumed_mapping_job(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'vault/_system/ledgers/incremental-ingest/run'
            native = root / 'native/source/snapshot-a/mapping'
            operation = {'contract': 'native-contract', 'workflow_mode': 'incremental_v1', 'board': 'map-a',
                         'incremental': {'run_id': 'run', 'scope_kind': 'source', 'scope_id': 'snapshot-a', 'stage': 'mapping'}}
            put(native / 'operation.json', operation)
            job = native / 'jobs/request-a.json'
            put(job, {'request': {'id': 'request-a'}})
            task = {'status': 'blocked', 'claimed': False, 'created_by': 'native-contract', 'body': json.dumps({
                'contract': 'native-contract', 'operation_sha256': hashlib.sha256((native / 'operation.json').read_bytes()).hexdigest(),
                'job': job.name, 'job_sha256': hashlib.sha256(job.read_bytes()).hexdigest()})}
            reader = lambda _: {'task-a': task}
            source = {'state': 'prepared', 'snapshot_id': 'snapshot-a'}
            run, control = {'run_id': 'run', 'state': 'active'}, {'state': 'paused'}
            result = retry_capability(source, root, run, control, native_reader=reader)
            self.assertTrue(result['allowed'])
            self.assertEqual(result['retry_kind'], 'mapping-unconsumed')
            put(native / 'jobs/request-a.attempt-1.json', {'grant': {}})
            self.assertFalse(retry_capability(source, root, run, control, native_reader=reader)['allowed'])
            failed = source_pipeline({'path': 'b.pdf', 'state': 'failed'}, root, run, control,
                {'source_outcomes': [{'path': 'b.pdf', 'status': 'failed', 'reason': 'unsupported security boundary'}]}, [], native_reader=lambda _: {})
            self.assertEqual(len(failed['pipeline']), 8)
            self.assertFalse(failed['retry']['allowed'])
            self.assertIn('unsupported security', failed['retry']['reason'])


if __name__ == '__main__':
    unittest.main()
