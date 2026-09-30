"""One bound deterministic exact-plan worker; no native CLI in worker isolation."""
import json
from pathlib import Path
import re
import time

from hermes_source_units import ContractError
from hermes_source_units.exact_planning import ExactPlanner
from hermes_source_units.source_units import _exclusive_lock, _vault_path
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard
from hermes_source_units.workflow_guard import WORKER_LOCK_TIMEOUT


def complete_exact(adapter, request):
    """Retry only rejected ledger commits, rebuilding checks/revision each time."""
    deadline = time.monotonic() + WORKER_LOCK_TIMEOUT
    while True:
        try:
            return adapter.worker_complete(request)
        except ContractError as exc:
            if exc.code not in ('LOCK_BUSY', 'REVISION_CONFLICT'):
                raise
            if time.monotonic() >= deadline:
                raise ContractError('LOCK_TIMEOUT', '$',
                    'exact-plan completion could not commit within the lock wait limit') from exc
            adapter.worker_check(request)
            time.sleep(.05)


def prepare_exact(adapter, request):
    if request['node'] != 'exact-plan':
        raise ContractError('ACCESS_DENIED', '$', 'exact planning requires exact-plan card')
    begun = adapter.worker_begin(request)
    bound = {**request, 'template_hash':begun['template_hash']}
    workflow = adapter.workflow.status(request['workflow_id'])
    coverage = begun['source_coverage']
    if coverage['pending'] or not coverage['ready']:
        raise ContractError('INCOMPLETE_COVERAGE', '$', 'planning requires all source outcomes')
    if workflow['batch_id']:
        return complete_exact(adapter, {**bound, 'batch_id':workflow['batch_id']})
    selector = workflow['scope']['knowledge_selector']
    fixed = re.search(r'fixed batch_id\s+([A-Za-z0-9_.:-]+)', selector)
    batch_id = request.get('batch_id') or (fixed.group(1).rstrip(';') if fixed else
                                         request['workflow_id'].removeprefix('ingest-'))
    if fixed and batch_id != fixed.group(1).rstrip(';'):
        raise ContractError('STALE_PLAN', '$.batch_id', 'scope specifies a different fixed batch ID')
    knowledge = adapter.workflow.knowledge
    registry_path = _vault_path(knowledge.vault, '_system/metadata/document-registry.json')
    registry = json.loads(registry_path.read_text(encoding='utf-8'))['registry_revision']
    inputs = {'actor':begun['actor'], 'batch_id':batch_id, 'registry_revision':registry,
        'sources':[{'resource_id':item['resource_id'], 'unit_set_id':item['unit_set_id']}
                   for item in workflow['source_outcomes'] if item['status']=='ready'],
        'scope_fingerprint':fingerprint(workflow['scope']), 'origin':{'kind':'generated'}}
    lock = _vault_path(knowledge.vault,
        f"_system/reports/exact-planning/.locks/{fingerprint(request['workflow_id'])}.lock")
    with _exclusive_lock(lock), worker_binding(bound):
        last_check = [0.0]
        def check():
            # Full workflow checks hash every raw source. Bound domain writes
            # independently recheck cancellation/card membership under the lock.
            if time.monotonic() - last_check[0] >= 10:
                adapter.worker_check(bound)
                last_check[0] = time.monotonic()
        planner = ExactPlanner(knowledge, inputs, check=check)
        plan = planner.prepare()
        knowledge._exact_planner = planner
        try:
            adapter.worker_check(bound)
            with workflow_write_guard(knowledge.vault, kinds=('exact-plan',), actor=begun['actor']):
                planned = knowledge.plan_batch(plan)
                planner._progress('batch_created', batch_id=batch_id)
        finally:
            knowledge._exact_planner = None
        adapter.worker_check(bound)
        result = complete_exact(adapter, {**bound, 'batch_id':batch_id})
        return {**result, 'batch_id':batch_id, 'task_count':len(plan['tasks']),
                'progress_ref':(planner.root/'progress.json').relative_to(knowledge.vault).as_posix(),
                'new_measurements':planner.measured, 'reused_measurements':planner.reused,
                'coverage_gaps':[item for item in workflow['source_outcomes'] if item['status']=='failed']}
