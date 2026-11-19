"""Regression checks for same-clause continuation and bounded evidence review."""
from pathlib import Path
import sys

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'hermes-obsidian-governed-ingest-orchestrator' / part)
                for part in ('lib', 'scripts')]

from hermes_source_units.incremental_pages import preparation_fingerprint
from hermes_source_units.validation import ContractError
from incremental_writing_amendment import classify
from incremental_writing_evidence import EvidenceCatalogue


def catalogue(units):
    result = EvidenceCatalogue.__new__(EvidenceCatalogue)
    result.aliases = {}
    offset = 0
    for alias, text, coverage, section in units:
        end = offset + len(text)
        result.aliases[alias] = {'alias': alias, 'snapshot': 'same', 'section': section, 'coverage': coverage,
                                 'text': text, 'locator': {'kind': 'text', 'path': 'document.md',
                                                          'span': {'start': offset, 'end': end}}}
        offset = end
    result.read = lambda item: item['text']
    return result


def test_numbered_clause_continues_across_registered_units():
    source = catalogue([
        ('anchor', '12.0.9 应符合下列规定：\n', 'mapped', '12'),
        ('first', '- 1 必须核对材料；\n- 2 应符合要求；\n', 'mapped', '12'),
        ('second', '- 3 应保存检查记录。\n', 'mapped', '12'),
        ('next', '12.0.10 另有要求。\n', 'mapped', '12'),
    ])
    assert source.clause_continuations({'anchor'}) == [('anchor', 'first'), ('anchor', 'second')]
    source.aliases['first']['coverage'] = 'held'
    with pytest.raises(ContractError, match='unavailable'):
        source.clause_continuations({'anchor'})


def test_citation_review_preserves_original_answer_and_inspection():
    mapping = {'m1': {'unit_ref': 'source'}}
    prepared = {'view': {}, 'material_aliases': mapping, 'support_refs': list(mapping.values()),
                'projection': {'mapping': mapping, 'view': {'materials': [{'ref': 'm1', 'role': 'core'}]}}}
    prepared['view']['preparation_fingerprint'] = preparation_fingerprint(prepared)
    fragment = {'prepared': prepared}
    answer = {'content': '该要求。〔m1〕', 'inspected': ['m1'], 'unresolved': []}
    reviewed = classify(fragment, answer)
    assert reviewed['state'] == 'valid' and reviewed['answer']['content'] == '该要求。[m1]'
    assert '〔m1〕' in answer['content']
    assert classify(fragment, {**answer, 'inspected': []})['state'] == 'held'
    assert classify(fragment, {**answer, 'content': '该要求。（m1、m2）'})['state'] == 'held'
