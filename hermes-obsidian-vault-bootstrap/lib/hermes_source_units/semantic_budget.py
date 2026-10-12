"""Versioned execution policy; estimates are planning heuristics, not tokens."""
from .validation import fingerprint

PHASE_BALANCED_POLICY={
    'version':'phase-balanced/v2','output_tokens':8192,'output_reserve':1024,
    'input_correction_reserve_codepoints':2048,'correction_slot_reserve_codepoints':512,'correction_message_codepoints':160,
    'candidate_core_bytes_per_estimated_token':6,
    'citation_prior_bytes_per_estimated_token':4,
    'citation_core_bytes_per_estimated_token':12,
    'task_framing_estimated_tokens':192,'inspection_estimated_tokens':96,'image_semantic_estimated_tokens':512,
    'material_dependencies':'explicit-registered-reading-dependencies/v1',
    'grouping':'contiguous-balanced; no task or material truncation'}


def budget_policy_pin():
    return {'version':PHASE_BALANCED_POLICY['version'],
            'configuration_sha256':fingerprint(PHASE_BALANCED_POLICY)}
