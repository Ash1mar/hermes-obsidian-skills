"""Native preflight acceptance requires terminal evidence from every worker."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('canary_gate', ROOT / 'hermes-source-units/tools/accept_governed_canary.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.mark.parametrize('missing', [None, 'bound_command', 'tool_result'])
@pytest.mark.parametrize('interface', ['legacy', 'fused', 'native'])
def test_preflight_audit_requires_all_eight_native_workers(tmp_path, missing, interface):
    database = tmp_path / 'state.db'
    snapshots = []
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE messages(session_id TEXT, role TEXT, content TEXT, tool_calls TEXT)')
        connection.execute('CREATE TABLE sessions(id TEXT, source TEXT)')
        for index in range(8):
            session = 'session-' + str(index)
            task_id = 'task-' + str(index)
            snapshots.append({'task': {'id': task_id, 'session_id': None},
                              'runs': [{'id': index, 'ended_at': 123}]})
            connection.execute('INSERT INTO sessions VALUES (?, ?)', (session, 'kanban'))
            lease = json.dumps({'worker_request': {'worker_id': 'ingest-worker-' + task_id}})
            connection.execute('INSERT INTO messages VALUES (?, ?, ?, ?)', (session, 'tool', lease, None))
            command = 'python3 domain.py --worker-binding lease.json batch-pass --request draft.json --validate-only'
            if interface == 'fused':
                command = command.replace('batch-pass', 'batch-submit').replace(' --validate-only', '')
            if index == 7 and missing == 'bound_command':
                command = command.replace('--worker-binding lease.json ', '')
            calls = json.dumps([{'function': {'name': 'terminal', 'arguments': {'command': command}}}])
            if interface == 'native':
                calls = json.dumps([{'function': {'name': 'ingest_submit_passes', 'arguments': {'vault': 'fixture', 'passes': []}}}])
                if index == 7 and missing == 'bound_command':
                    calls = '[]'
            connection.execute('INSERT INTO messages VALUES (?, ?, ?, ?)', (session, 'assistant', '', calls))
            response = json.dumps({'output': json.dumps({'validated': True, 'batch': {}, 'results': [], 'failures': []})})
            if interface != 'legacy':
                response = json.dumps({'validated': True, 'results': [{'preflight_ref': 'audit.json', 'pass_id': 'a'*64}], 'failures': []})
            role = 'assistant' if index == 7 and missing == 'tool_result' else 'tool'
            connection.execute('INSERT INTO messages VALUES (?, ?, ?, ?)', (session, role, response, None))
    before = database.read_bytes()
    if missing:
        with pytest.raises(RuntimeError, match='no successful bound preflight'):
            gate.native_preflight_evidence(tmp_path, snapshots)
    else:
        evidence = gate.native_preflight_evidence(tmp_path, snapshots)
        assert len(evidence) == 8
        assert all(item['preflight_tool_calls'] == item['successful_result_messages'] == 1 for item in evidence)
    assert database.read_bytes() == before
