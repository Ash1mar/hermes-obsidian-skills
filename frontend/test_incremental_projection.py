"""Incremental UI keeps native source progress and shared round scope explicit."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from incremental import fingerprint
from incremental_node_recovery import source_actions, validate_action
from incremental_projection import overall_stages, retry_capability, round_projection, source_pipeline


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


class PipelineProjectionTests(unittest.TestCase):
    def test_mapping_hold_reports_missing_native_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'vault/_system/ledgers/incremental-ingest/run'
            source = {'path': 'source.pdf', 'state': 'mapping_held', 'snapshot_id': 'snapshot',
                      'mapping_hold': {'execution_hold': {'ref': 'execution-hold.json'}}}
            view = source_pipeline(source, root, {'state': 'active'}, {'state': 'active'}, {}, [],
                                   native_reader=lambda _: {})
            mapping = next(row for row in view['pipeline'] if row['key'] == 'mapping')
            self.assertEqual((mapping['state'], mapping['detail']), ('held', '必需 Map 任务缺少可核验回执'))

    def test_ordinary_writing_reports_independent_section_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            put(root / 'rounds/round-1/writing-disposition.json', {
                'written': [{'preparation': 'qualified-section'}], 'original_pages': 0,
                'independent_sections': [{'name': '4.1 合格小节'}], 'held': [{'change_id': 'parent'}],
            })
            round_ = round_projection(root, 'round-1', {'active_round_id': 'round-1'}, {}, native_reader=lambda _: {})
            self.assertEqual(round_['original_pages_written'], 0)
            self.assertEqual(round_['independent_section_candidates'], 1)
            self.assertEqual(next(row for row in round_['stages'] if row['key'] == 'writing')['state'], 'partial')
            self.assertEqual(round_['writing_held'], [{'change_id': 'parent'}])

    def test_network_pause_exposes_and_binds_only_the_current_round(self):
        from incremental import command, progress

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            directory, vault = base / 'console', base / 'vault'
            request = {'operation_id': 'run', 'actor': 'operator', 'vault_path': str(vault),
                       'vault_name': 'Fixture', 'sources': [], 'retrieval_policy': {'query_enabled': False}}
            put(directory / 'request.json', request)
            root = vault / '_system/ledgers/incremental-ingest/run'
            run = {'state': 'active', 'revision': 8, 'active_round_id': 'round-1', 'round_ids': ['round-1']}
            put(root / 'run.json', run)
            boundary = {'scope': {'scope_kind': 'round', 'scope_id': 'round-1'},
                        'stage': 'network_recovery', 'evidence': {'phase': 'writing', 'run_revision': 8}}
            control = {'state': 'paused', 'revision': 4, 'active_scopes': {}, 'boundary': boundary}
            put(root / 'control/head.json', control)
            attempts = [{'id': 'previous'}]
            view = progress(directory, request, attempts, False, False, True)
            action, = view['nodes'][0]['actions']
            self.assertEqual((action['id'], action['label'], action['round_id']),
                             ('incremental-continue-round', '继续本轮', 'round-1'))
            self.assertEqual(action['boundary_fingerprint'], fingerprint(boundary))
            writing = next(row for row in view['incremental']['overall_stages'] if row['key'] == 'writing')
            self.assertEqual(writing['state'], 'paused')
            self.assertTrue(writing['network_recovery'])
            self.assertFalse(progress(directory, request, attempts, True, False, True)['nodes'][0]['actions'])

            skills = base / 'skills'
            put(skills / 'hermes-obsidian-governed-ingest-orchestrator/scripts/manage_incremental_ingest.py', {})
            config = {'skills_root': str(skills)}
            attempt = {**action, 'action': action['id'], 'id': 'continue'}
            argv = command(directory, attempt, config, put)
            sent = json.loads(Path(argv[-1]).read_bytes())
            self.assertEqual(sent['action'], 'continue')
            self.assertEqual(sent['network_recovery'], {'round_id': 'round-1', 'run_revision': 8})
            with self.assertRaisesRegex(ValueError, '轮次已经变化'):
                command(directory, {**attempt, 'id': 'stale', 'round_id': 'round-2'}, config, put)
            replay = {**attempt, 'id': 'duplicate'}
            self.assertEqual(json.loads(Path(command(directory, replay, config, put)[-1]).read_bytes()), sent)
            operation_id = sent['operation_id']
            put(root / 'control/operations' / (operation_id + '.json'),
                {'commit_state': 'prepared', 'request_sha256': fingerprint(sent)})
            put(root / 'control/head.json', {**control, 'state': 'active', 'revision': 5, 'boundary': None,
                                           'network_continuation': {'operation_id': operation_id}})
            self.assertEqual(json.loads(Path(command(directory, replay, config, put)[-1]).read_bytes()), sent)
            put(root / 'control/head.json', {**control, 'boundary': {**boundary, 'stage': 'writing'}})
            view = progress(directory, request, attempts, False, False, True)
            self.assertEqual([row['id'] for row in view['nodes'][0]['actions']], ['incremental-continue'])
            with self.assertRaisesRegex(ValueError, '轮次已经变化'):
                command(directory, replay, config, put)

    def test_closure_review_binds_latest_control_and_exposes_confirmation(self):
        from incremental import closure_node, closure_ready

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'console'
            root = Path(folder) / 'vault/_system/ledgers/incremental-ingest/run'
            run = {'state': 'active', 'revision': 8, 'reservations': {'one': {'state': 'reserved', 'phase': 'mapping'}},
                   'map_result_ids': ['map'], 'consumed_map_ids': ['map'], 'round_ids': ['round-1']}
            put(root/'rounds/round-1/settlement.json', {'state': 'delivered'})
            put(Path(folder)/'vault/_system/knowledge-releases/release-1/manifest.json', {'subjects': [{}] * 15})
            rounds = [{'delivery_state': 'committed', 'release_id': 'release-1'}]
            control = {'state': 'active', 'revision': 2, 'active_scopes': {}}
            self.assertTrue(closure_ready(root, run, control, interrupted=True))
            self.assertFalse(closure_ready(root, run, control, interrupted=False))
            node = closure_node(directory, root, run, control, rounds, [], ready=True, available=True)
            self.assertEqual(node['detail'], '1 轮已验收，15 页已发布，1 笔待对账')
            self.assertEqual([a['id'] for a in node['actions']], ['incremental-closure-preview'])
            boundary = {'stage': 'closure', 'evidence': {'published_pages': 15}}
            control.update(state='paused', revision=3, boundary=boundary)
            attempts = [{'action': 'incremental-closure-preview', 'id': 'preview', 'returncode': 0}]
            review = {'request': {'run_revision': 8, 'expected_revision': 3, 'boundary_fingerprint': fingerprint(boundary)},
                      'report_sha256': 'preview-hash', 'reservations': {'one': {'phase': 'mapping'}}}
            put(directory/'attempts/preview/closure-preview.json', review)
            node = closure_node(directory, root, run, control, rounds, attempts, ready=True, available=True)
            self.assertEqual(node['actions'][-1]['report_sha256'], 'preview-hash')
            self.assertEqual(node['reservations'], review['reservations'])
            node = closure_node(directory, root, run, {**control, 'revision': 4}, rounds, attempts, ready=True, available=True)
            self.assertEqual([a['id'] for a in node['actions']], ['incremental-closure-preview'])

    def test_source_nodes_bind_preparation_to_file_and_writing_to_dependent_fragments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'vault/_system/ledgers/incremental-ingest/run'
            rid = 'round-1'
            run = {'run_id': 'run', 'state': 'active', 'revision': 5, 'active_round_id': rid, 'source_manifest': [
                {'path': 'a.pdf', 'sha256': 'a' * 64}, {'path': 'b.pdf', 'sha256': 'b' * 64}]}
            control = {'state': 'active', 'revision': 4, 'active_scopes': {}}
            node = 'source-prepare:' + fingerprint({'path': 'b.pdf', 'content_sha256': 'b' * 64})[:16]
            workflow = {'workflow_id': 'workflow', 'kanban': {'board_id': 'sources', 'task_map': [
                {'node': node, 'task_id': 'task-b', 'idempotency_key': 'binding-key'}]}}
            task = {'status': 'scheduled', 'claimed': False, 'last_outcome': 'scheduled',
                    'last_run_id': 7, 'last_started_at': 1, 'last_ended_at': 1}
            reader = lambda _: {'task-b': task}
            queued = source_pipeline({'path': 'b.pdf', 'state': 'pending', 'actions': []}, root, run, control,
                                     workflow, [], native_reader=reader)
            source_actions(queued, root, run, control, {}, 'prior', writable=True, interrupted=False)
            self.assertEqual(queued['pipeline'][0]['state'], 'waiting-dispatch')
            self.assertFalse(queued['pipeline'][0]['actions'])

            task.update(status='blocked', last_outcome='worker_exited', last_ended_at=2)
            put(root.parent.parent / 'ingest-workflows/workflow/bindings' / (fingerprint('binding-key') + '.execution.json'),
                {'state': 'running', 'executor': 'program-v1', 'run_id': 7,
                 'grant': {'worker_pid': 37, 'claim_lock': 'native-claim'}})
            failed = source_pipeline({'path': 'b.pdf', 'state': 'pending', 'actions': []}, root, run, control,
                                     workflow, [], native_reader=reader)
            source_actions(failed, root, run, control, {}, 'prior', writable=True, interrupted=False)
            prep = failed['pipeline'][0]['actions'][0]
            self.assertEqual((prep['retry_kind'], prep['source_path']), ('preparation-unfinished', 'b.pdf'))
            self.assertFalse(failed['pipeline'][1]['actions'])

            writer = root / 'native/round' / rid / 'writing'
            round_dir = root / 'rounds' / rid
            put(round_dir / 'input.json', {'snapshots': ['snap-a', 'snap-b'], 'map_result_ids': ['map-a']})
            changes = [{'change_id': f'change-{key}', 'page_id': f'page-{key}', 'name': key, 'action': 'create',
                        'coverage_scope': {'snapshots': [f'snap-{key}']}} for key in ('a', 'b')]
            put(round_dir / 'page-changes.json', changes)
            put(round_dir / 'writing-disposition.json', {'written': [], 'held': changes})
            for key in ('a', 'b'):
                put(writer / 'fragment-plans' / f'change-{key}.json', {'fragments': [{'request': {'id': key * 64}}]})
                put(writer / 'jobs' / f'{key * 64}.result.json', {'state': 'settled', 'attempts': [{'state': 'returned'}]})
            control = {**control, 'state': 'paused', 'pause_after': ['writing'], 'boundary': {
                'scope': {'scope_kind': 'round', 'scope_id': rid}, 'stage': 'writing'}}
            round_ = round_projection(root, rid, run, control, native_reader=reader)
            source = {'path': 'a.pdf', 'state': 'mapped', 'snapshot_id': 'snap-a', 'map_result_id': 'map-a', 'actions': []}
            item = source_pipeline(dict(source), root, run, control, workflow, [round_], native_reader=reader)
            source_actions(item, root, run, control, {rid: round_}, 'prior', writable=True, interrupted=False)
            writing = next(action for action in item['actions'] if action['id'] == 'incremental-node-writing')
            self.assertEqual(writing['fragment_ids'], ['a' * 64])
            validate_action({**writing, 'revise_fragments': ['a' * 64]}, writing, ValueError)
            for override in ({'round_id': 'round-2', 'revise_fragments': ['a' * 64]}, {'revise_fragments': ['b' * 64]}):
                with self.assertRaises(ValueError):
                    validate_action({**writing, **override}, writing, ValueError)

            closed = {**run, 'state': 'partial', 'active_round_id': None}
            changes[0].update(action='hold', reason='Original hold')
            put(round_dir / 'page-changes.json', changes)
            round_ = round_projection(root, rid, closed, control, native_reader=reader)
            item = source_pipeline({**source, 'actions': []}, root, closed, control, workflow, [round_], native_reader=reader)
            source_actions(item, root, closed, control, {rid: round_}, 'prior', writable=False, interrupted=False)
            self.assertFalse(item['actions'])
            self.assertEqual(item['pipeline'][2]['recovery_scopes'][0]['blocked_pages'][0]['reason'], 'Original hold')

    def test_delivery_nodes_stop_at_their_own_shared_prefix(self):
        from incremental_node_recovery import delivery_recovery
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rid = 'round-1'
            put(root / 'rounds/round-1/formal-request.json', {})
            for phase, prefix, stop in (('formal-build', 'build_committing', 'build_committed'),
                                        ('release', 'index_pending', 'index_verified'),
                                        ('acceptance', 'index_verified', None)):
                round_ = {'round_id': rid, 'delivery_state': prefix}
                self.assertEqual(delivery_recovery(root, phase, round_), {'stop_after': stop})
                self.assertIsNone(delivery_recovery(root, phase, {**round_, 'delivery_state': 'committed'}))

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
