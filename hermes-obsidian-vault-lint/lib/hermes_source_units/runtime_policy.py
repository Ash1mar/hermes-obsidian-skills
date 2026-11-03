"""Portable per-workflow settings; host endpoints and credentials stay external."""
from pathlib import Path
import hashlib
import json

from .validation import ContractError

POLICY_REF = '_system/metadata/retrieval-policy.json'


def canary_count(service, value):
    if value['scope'].get('canary_policy') == 'off':
        return 0
    if value['scope'].get('canary_policy') == 'sample':
        count = min(8, len(service.knowledge._slices(value['batch_id'])))
        if not count:
            raise ContractError('CANARY_UNAVAILABLE', '$', 'no planned slices')
        return count
    return 8


def vault_provider_config(config, vault, role):
    """Only opt-in/opt-out is portable. A host kill switch always wins."""
    path = Path(vault) / POLICY_REF
    if not path.exists():
        return config
    if path.is_symlink():
        raise ValueError('retrieval policy must be a regular Vault file')
    policy = json.loads(path.read_text(encoding='utf-8'))
    if (set(policy) != {'contract', 'sync_enabled', 'query_enabled'}
            or policy['contract'] != 'vault-retrieval-policy/v1'
            or any(type(policy[k]) is not bool for k in ('sync_enabled','query_enabled'))):
        raise ValueError('invalid Vault retrieval policy')
    return {**config, 'enabled': bool(config.get('available', True)) and policy[role+'_enabled']}


def check_provider_pin(vault, scope):
    expected = scope.get('provider_policy_sha256')
    if expected:
        path = Path(vault) / POLICY_REF
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ContractError('STALE_INPUT', '$', 'Vault Provider policy changed during workflow')
