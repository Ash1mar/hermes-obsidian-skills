"""Immutable recovery intent for new workflows; legacy workflows keep fallback."""
from copy import deepcopy
from .validation import ContractError
from .qa_policy import POLICY as QA

DEFAULT = {
    'contract': 'workflow-recovery-policy/v1',
    'semantic_failure': 'once_then_isolate',
    'qa': QA,
    'source_resource_retries': 1,
    'controller_attempts': 3,
    'unknown_completion': 'isolate_after_drain',
    'initial_release': 'scoped_bootstrap',
}


def validate(policy):
    expected = deepcopy(DEFAULT)
    expected['semantic_failure'] = policy.get('semantic_failure')
    expected['initial_release'] = policy.get('initial_release')
    expected['unknown_completion'] = policy.get('unknown_completion')
    if (policy != expected or expected['semantic_failure'] not in ('hold', 'once_then_isolate')
            or expected['initial_release'] not in ('hold', 'scoped_bootstrap')
            or expected['unknown_completion'] not in ('hold', 'isolate_after_drain')):
        raise ContractError('INVALID_SCHEMA', '$.scope.recovery_policy', 'unsupported recovery policy')
    return deepcopy(policy)


def effective(workflow, configuration):
    pinned = workflow.get('scope', {}).get('recovery_policy')
    if pinned is not None:
        policy = validate(pinned)
        from .validation import fingerprint
        from .ingest_workflow import mutation_digest
        for request in workflow.get('unknown_isolation_adoptions', []):
            if (request.get('workflow_id') != workflow.get('workflow_id')
                    or request.get('actor') != workflow.get('actor')
                    or request.get('previous_policy_sha256') != fingerprint(policy)
                    or request.get('input_digest') != mutation_digest(request)
                    or request.get('unknown_completion') != 'isolate_after_drain'):
                raise ContractError('STALE_INPUT', '$', 'unknown isolation adoption changed')
            policy['unknown_completion'] = 'isolate_after_drain'
        return policy
    if configuration.get('qa_policy', QA) != QA:
        raise ContractError('INVALID_SCHEMA', '$', 'unsupported automatic QA policy')
    legacy = deepcopy(DEFAULT)
    legacy.update(semantic_failure=configuration.get('pass_failure_policy', 'hold'),
                  source_resource_retries=0, initial_release='legacy_recovery', unknown_completion='hold')
    return legacy
