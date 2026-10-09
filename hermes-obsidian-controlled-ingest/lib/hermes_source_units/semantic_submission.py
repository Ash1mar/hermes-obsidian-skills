"""Typed bounded semantic payloads, shared by CLI and native tool adapters."""
from __future__ import annotations

import copy

from .validation import _check_shape, load_schema


def closed(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def submission_schema(*, confirmation=False):
    """Inline the canonical semantic field types; no second schema to drift."""
    defs = load_schema()['$defs']
    def inline(node):
        if isinstance(node, list):
            return [inline(v) for v in node]
        if not isinstance(node, dict):
            return node
        if '$ref' in node:
            return inline(defs[node['$ref'].removeprefix('#/$defs/')])
        return {k: inline(v) for k, v in node.items()}
    reference = {'oneOf': [{'type': 'string', 'minLength': 1},
        closed({'ref': {'type': 'string', 'minLength': 1},
                'span': closed({'start': {'type': 'integer', 'minimum': 0},
                                'end': {'type': 'integer', 'minimum': 0}})})]}
    identity = {'task': {'type': 'string', 'pattern': '^t[1-9][0-9]*$'},
                'input_id': {'type': 'string', 'minLength': 1}}
    if confirmation:
        item = closed({**identity,
            'candidate_pass_id': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
            'decision': {'type': 'string', 'const': 'confirmed_unchanged'},
            'review_note': {'type': 'string', 'minLength': 1}})
        return closed({'confirmations': {'type': 'array', 'items': item, 'minItems': 1}})
    inspection = inline(defs['inspection'])
    inspection['properties']['source_ref'] = copy.deepcopy(reference)
    candidate = inline(defs['candidate_observation'])
    candidate['properties']['support_refs']['items'] = copy.deepcopy(reference)
    item = closed({**identity, 'sequence': {'type': 'integer', 'minimum': 0},
        'inspections': {'type': 'array', 'items': inspection, 'minItems': 1},
        'candidates': {'type': 'array', 'items': candidate},
        'empty_reason': {'type': 'string'}})
    return closed({'passes': {'type': 'array', 'items': item, 'minItems': 1}})


def validate_submission(request, *, confirmation=False):
    _check_shape(submission_schema(confirmation=confirmation), request, {}, '$')


def native_submission_schema(*, confirmation=False):
    """Native handles resolve identities; legacy explicit identities stay checked."""
    schema = submission_schema(confirmation=confirmation)
    key = 'confirmations' if confirmation else 'passes'
    item = schema['properties'][key]['items']
    for field in ('input_id', 'candidate_pass_id'):
        if field in item['required']:
            item['required'].remove(field)
            item['properties'][field]['description'] = 'Legacy compatibility only. Omit: the program resolves the observed identity.'
    return schema


def validate_native_submission(request, *, confirmation=False):
    _check_shape(native_submission_schema(confirmation=confirmation), request, {}, '$')
