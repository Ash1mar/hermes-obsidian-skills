"""Deterministic domain work on a canonical native Kanban binding."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.validation import fingerprint
from hermes_source_units.vault_finalize import FileVaultFinalizeService
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard
from ingest_kanban import PROGRAM_ASSIGNEE as ASSIGNEE, PROGRAM_KINDS as KINDS, program_template, executor_for

MARKER = '<!-- hermes-program-worker/v1 -->'


def admission_check(adapter, request):
    """Check a ready card's authority before claim; no lease exists yet."""
    if not request['node'].startswith('pass-slice:'):
        return adapter.worker_check(request)
    adapter.validate_worker_request(request, beginning=True)
    workflow, value = adapter._slice_worker(request['workflow_id'], request['node'], request['task_id'])
    if value['state'] != 'ready':
        raise ContractError('PASS_ADMISSION_WAIT', '$', 'Pass admission waits for a ready unleased slice')
    return {'ok':True, 'node':request['node'], 'batch_id':workflow['batch_id'], 'slice_id':value['slice_id']}


def canonical_binding(adapter, binding, *, executor='program-v1'):
    path = Path(binding).resolve()
    request = _load_json(path)
    adapter.validate_worker_request(request)
    value = adapter.workflow.status(request['workflow_id'])
    cards = [c for c in value['kanban']['task_map'] if c.get('task_id') == request['task_id']
             and c['node'] == request['node']]
    if len(cards) != 1 or path != _vault_path(adapter.workflow.vault,
            f"_system/ledgers/ingest-workflows/{value['workflow_id']}/bindings/{fingerprint(cards[0]['idempotency_key'])}.json").resolve():
        raise ContractError('STALE_INPUT', '$', 'binding is not the current dispatcher artifact')
    kind = request['node'].partition(':')[0]
    templates = adapter.workflow.pinned_templates(value)
    # Executor changes are part of the immutable template, never inferred from
    # a newly installed script or an editable native card body.
    content = (adapter.workflow.revision_instructions(value)
        if adapter.workflow.revision_allows(value, request['node']) else
        (adapter.workflow.execution_template(value, request['node'].partition(':')[2])
         if kind == 'pass-slice' else None) or templates.get(kind))
    if executor_for(kind, content) != executor:
        raise ContractError('ACCESS_DENIED', '$', 'pinned contract does not authorize a program worker')
    return request, value


def sync_provider(adapter, bound, value):
    script = Path(__file__).resolve().parents[2] / 'hermes-obsidian-knowledge-finalize/scripts/sync_release_index.py'
    spec = importlib.util.spec_from_file_location('bound_release_index', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, config = module.provider_config(None)
    if config.get('enabled', True) is False:
        raise ContractError('PROVIDER_DISABLED', '$', 'requested sync has no enabled Provider; no command or model started')
    release_id, release_hash = module.release_identity(adapter.workflow.vault)
    if release_id != value['release_id']:
        raise ContractError('STALE_INPUT', '$', 'Provider target differs from the bound release')
    transport = str(config.get('transport') or 'command')
    adapter.worker_check(bound)
    if transport == 'command':
        payload = module.call_command(config, adapter.workflow.vault, False, release_id, release_hash)
    elif transport == 'http':
        payload = module.call_http(config, False, release_id, release_hash)
    else:
        raise ContractError('INVALID_SCHEMA', '$', 'unsupported Provider transport')
    if (payload.get('status') != 'ready' or payload.get('protocol_version') != module.PROTOCOL_VERSION
            or payload.get('release_id') != release_id or payload.get('release_hash') != release_hash):
        raise ContractError('STALE_INPUT', '$', 'Provider did not return the exact ready release')
    path = _vault_path(adapter.workflow.vault, '_system/reports/retrieval-index-manifest.json')
    with worker_binding(bound), workflow_write_guard(adapter.workflow.vault,
            kinds=('provider-sync',), actor=value['actor']):
        if module.release_identity(adapter.workflow.vault) != (release_id, release_hash):
            raise ContractError('STALE_INPUT', '$', 'release changed during Provider sync')
        previous = module.load_json(path) if path.is_file() else None
        module.write_manifest(path, module.portable_manifest(payload, transport, previous))


def execute(adapter, binding):
    request, value = canonical_binding(adapter, binding)
    kind = request['node'].partition(':')[0]
    adapter.worker_check(request)
    if kind == 'source-prepare':
        from source_preparation import prepare_source
        return prepare_source(adapter, request)
    if kind == 'exact-plan':
        from exact_preparation import prepare_exact
        return prepare_exact(adapter, request)
    begun = adapter.worker_begin(request)
    bound = {**request, 'template_hash': begun['template_hash']}
    value = adapter.workflow.status(request['workflow_id'])
    if kind == 'vault-finalize-plan':
        service = FileVaultFinalizeService(adapter.workflow.vault)
        batch = adapter.workflow.knowledge._batch(value['batch_id'])
        release_id = 'release-' + fingerprint({'workflow_id':value['workflow_id'],
            'node':request['node'], 'input_fingerprint':request['input_fingerprint']})[:24]
        with worker_binding(bound), workflow_write_guard(adapter.workflow.vault,
                kinds=(kind,), actor=value['actor']):
            # Recover the same valid plan after interruption; do not include
            # other workflows' runs or recompute a new release identity.
            path = service._plan_path(release_id)
            plan = _load_json(path) if path.is_file() else service.plan({
                'actor':value['actor'], 'release_id':release_id,
                'expected_state_revision':service._state()['revision'],
                'build_run_ids':batch['run_ids']})['plan']
        bound.update(release_id=release_id, plan_id=plan['plan_id'])
    elif kind == 'release-apply':
        service = FileVaultFinalizeService(adapter.workflow.vault)
        with worker_binding(bound), workflow_write_guard(adapter.workflow.vault,
                kinds=(kind,), actor=value['actor']):
            plan = _load_json(service._plan_path(value['release_id']))
            if (plan['plan_id'] != value['release_plan_id']
                    or value['checkpoints']['checkpoint_2']['state'] != 'approved'):
                raise ContractError('STALE_INPUT', '$', 'release lacks the exact checkpoint approval')
            service.apply({'actor':value['actor'], 'release_id':value['release_id'],
                'plan_id':value['release_plan_id'], 'expected_revision':plan['revision']})
    elif kind == 'provider-sync':
        sync_provider(adapter, bound, value)
    # Checkpoint/acceptance completion already performs program validators,
    # hashes the decisions and establishes durable pauses. No invented review.
    return adapter.worker_complete(bound)
