"""Preserve authenticated fragment ownership across a stopped native writer."""
from pathlib import Path
import sys

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator' / part)
                for part in ('lib', 'scripts')]

from incremental_writing_resume import match_slots


def slot(key, owner, *dependencies):
    refs = [owner, *dependencies]
    material = {f'm{i}': {'unit_ref': ref} for i, ref in enumerate(refs, 1)}
    return {'request': {'id': key}, 'prepared': {
        'support_refs': [{'unit_ref': owner}], 'material_aliases': material,
        'projection': {'mapping': material,
                       'view': {'materials': [{'ref': alias, 'role': 'core'} for alias in material]}}}}


def test_resume_reuses_identical_receipts_and_dispatches_only_independent_unattempted_scope():
    first = [slot('known', 'a'), slot('unknown', 'b'), slot('dependent', 'c'),
             slot('pending', 'd'), slot('held', 'e')]
    revised = [slot('adopt', 'a'), slot('uncertain', 'b'), slot('dependent-new', 'c', 'b'),
               slot('fresh', 'd'), slot('cannot-redo', 'e')]
    history = {part['request']['id']: {'state': state} for part, state in zip(
        first, ('valid', 'isolated_attempt', 'unattempted', 'unattempted', 'held'), strict=True)}
    decisions = match_slots(first, revised, history)
    assert [decision['disposition'] for decision in decisions] == ['reuse', 'held', 'held', 'dispatch', 'held']
    assert decisions[0]['prior_request'] == 'known'
    assert 'required reading' in decisions[2]['reason']


def test_resume_will_not_rebind_valid_response_to_changed_source_inputs():
    original = slot('prior', 'a')
    changed = slot('new', 'a', 'other-table')
    history = {'prior': {'state': 'valid'}}
    decision, = match_slots([original], [changed], history)
    assert decision['disposition'] == 'held'
