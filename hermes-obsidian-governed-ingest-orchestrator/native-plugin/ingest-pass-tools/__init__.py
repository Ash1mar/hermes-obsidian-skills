"""Native Hermes tools; workers author semantics, adapters own mechanics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def register(ctx):
    root = Path(ctx.get_config('skills_root', str(Path(__file__).resolve().parents[2] / 'skills/domain')))
    ingest = root / 'hermes-obsidian-controlled-ingest'
    orchestrator = root / 'hermes-obsidian-governed-ingest-orchestrator'
    sys.path.insert(0, str(ingest / 'lib'))
    from hermes_source_units.semantic_submission import submission_schema, validate_submission
    from hermes_source_units.validation import _check_shape

    def available():
        return bool(os.environ.get('HERMES_KANBAN_TASK') and os.environ.get('HERMES_KANBAN_WORKSPACE')
                    and (ingest / 'scripts/manage_knowledge_build.py').is_file())

    def parameters(confirmation=False):
        schema = submission_schema(confirmation=confirmation)
        schema['properties']['vault'] = {'type': 'string', 'minLength': 1}
        schema['required'].append('vault')
        return schema

    def invoke(args, *, confirmation=False, heartbeat=False):
        temporary = None
        try:
            if not available():
                raise ValueError('typed Pass tools require a native Kanban worker workspace')
            if heartbeat:
                _check_shape(heartbeat_schema, args, {}, '$')
            else:
                _check_shape(parameters(confirmation), args, {}, '$')
                payload = {k: v for k, v in args.items() if k != 'vault'}
                validate_submission(payload, confirmation=confirmation)
            workspace = Path(os.environ['HERMES_KANBAN_WORKSPACE']).resolve(strict=True)
            binding = workspace / 'worker-request.json'
            if binding.is_symlink() or not binding.is_file():
                raise ValueError('missing regular checked worker-request.json; run bound worker-begin first')
            request = json.loads(binding.read_text(encoding='utf-8'))
            if request.get('task_id') != os.environ['HERMES_KANBAN_TASK']:
                raise ValueError('binding belongs to another native task')
            # No shell, no model-authored command or identity fields. The existing
            # dispatcher checks live contracts and atomically saves the new lease.
            refreshed = subprocess.run([sys.executable, str(orchestrator / 'scripts/dispatch_ingest_workflow.py'),
                '--vault', args['vault'], 'worker-heartbeat', '--request', str(binding)],
                capture_output=True, text=True, encoding='utf-8', timeout=600)
            if refreshed.returncode:
                return refreshed.stderr.strip() or refreshed.stdout.strip()
            if heartbeat:
                result = json.loads(refreshed.stdout)
                return json.dumps({'ok': True, 'lease_revision': result['worker_request']['expected_revision']})
            fd, name = tempfile.mkstemp(prefix='.semantic-submit-', suffix='.json', dir=workspace)
            temporary = Path(name)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.write('\n')
            completed = subprocess.run([sys.executable, str(ingest / 'scripts/manage_knowledge_build.py'),
                '--vault', args['vault'], '--worker-binding', str(binding),
                'batch-confirm-citations' if confirmation else 'batch-submit', '--request', str(temporary)],
                capture_output=True, text=True, encoding='utf-8', timeout=600)
            return completed.stdout.strip() or completed.stderr.strip()
        except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            result = {'ok': False, 'error': str(exc)}
            if hasattr(exc, 'code'):
                result.update(code=exc.code, path=exc.path)
            return json.dumps(result, ensure_ascii=False)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    heartbeat_schema = {'type': 'object', 'properties': {'vault': {'type': 'string', 'minLength': 1}},
                        'required': ['vault'], 'additionalProperties': False}
    for name, description, schema, handler in (
        ('ingest_submit_passes', 'Submit actual bounded candidate or revised citation semantics. '
         'conditions/exceptions/support_refs are arrays. Checks and persists in one operation; '
         'never separately repeat a successful preflight.', parameters(), lambda args, **kw: invoke(args)),
        ('ingest_confirm_citations', 'Only after reviewing the persisted candidate against authorized evidence and QA, '
         'explicitly confirm its facts, conditions, exceptions and citations are unchanged. '
         'Provide its exact candidate_pass_id and actual review_note. Backend preserves full semantic and audit records. '
         'For changes use ingest_submit_passes.', parameters(True), lambda args, **kw: invoke(args, confirmation=True)),
        ('ingest_pass_heartbeat', 'Renew this live worker lease and save its checked binding. '
         'Submission tools also do this automatically; use during long reading between submissions.',
         heartbeat_schema, lambda args, **kw: invoke(args, heartbeat=True)),
    ):
        ctx.register_tool(name=name, toolset='ingest_pass', schema={'name': name, 'description': description,
                          'parameters': schema}, handler=handler, check_fn=available)
