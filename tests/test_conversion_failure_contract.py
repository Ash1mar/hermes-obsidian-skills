"""Conservative typed failures at the CLI boundary and governed source outcomes."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from test_ingest_worker_recovery import setup_worker
from test_p3_knowledge_build import ROOT
from hermes_source_units import ContractError
from source_preparation import prepare_source

spec = importlib.util.spec_from_file_location('pdf_converter_contract', ROOT /
    'hermes-obsidian-controlled-ingest/scripts/convert_pdf_with_mineru_bundle.py')
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


@pytest.mark.parametrize('diagnostic', ['Data format error', 'Password error',
                                      'Unsupported security scheme error'])
def test_terminal_typed_document_failure(diagnostic):
    terminal = f'pypdfium2._helpers.misc.PdfiumError: Failed to load document (PDFium: {diagnostic}).'
    output = 'Traceback (most recent call last):\n  File "parser.py", line 1\n' + terminal + '\n'
    assert converter.document_load_failure(output) == terminal


@pytest.mark.parametrize('output', [
    'unsupported security scheme',
    'PdfiumError: Failed to load document (PDFium: Password error).',
    'Traceback (most recent call last):\nMemoryError: document load failed',
    'Traceback (most recent call last):\nPdfiumError: Failed to load document (PDFium: File access error).',
    'Traceback (most recent call last):\nPdfiumError: Failed to load document (PDFium: Data format error).\nMinerU supervisor: cleanup incomplete',
    'Traceback (most recent call last):\nRuntimeError: model unavailable',
])
def test_unknown_environment_and_nonterminal_errors_remain_execution_failures(output):
    assert converter.document_load_failure(output) is None


def failed_conversion(argv, *, status='document_failed', change=None):
    destination = Path(argv[argv.index('-o') + 1])
    destination.mkdir(parents=True)
    source = Path(argv[2])
    from argparse import Namespace
    converter.write_conversion_result(Namespace(input=source,
        backend=argv[argv.index('--backend') + 1]), destination, status,
        reason='typed document load exception', engine_exit=1)
    if change:
        path = destination / 'conversion-result.json'
        value = json.loads(path.read_text()); value.update(change)
        path.write_text(json.dumps(value))
    return subprocess.CompletedProcess(argv, 4 if status == 'document_failed' else 1)


def test_two_proven_document_failures_close_only_assigned_source_and_keep_evidence(tmp_path, monkeypatch):
    target, adapter, value, req = setup_worker(tmp_path)
    calls = []
    def convert(argv, **kwargs):
        calls.append(argv)
        return failed_conversion(argv)
    monkeypatch.setattr('source_preparation.subprocess.run', convert)
    result = prepare_source(adapter, req)
    assert result['ok'] and len(calls) == 2
    latest = adapter.workflow.status(value['workflow_id'])
    assert len(latest['source_outcomes']) == 1
    outcome = latest['source_outcomes'][0]
    assert outcome['status'] == 'failed' and outcome['error_code'] == 'CONVERSION_FAILED'
    assert len(outcome['artifact_refs']) == 4
    assert all((target / ref).is_file() for ref in outcome['artifact_refs'])
    adapter._sync_current(value['workflow_id'])
    assert req['task_id'] in adapter.kanban.completed
    assert adapter.workflow.source_coverage(latest)['pending']


def test_runtime_failure_after_document_retry_preserves_pending_source(tmp_path, monkeypatch):
    _, adapter, value, req = setup_worker(tmp_path)
    calls = []
    def convert(argv, **kwargs):
        calls.append(argv)
        return failed_conversion(argv, status='document_failed' if len(calls) == 1 else 'runtime_failed')
    monkeypatch.setattr('source_preparation.subprocess.run', convert)
    with pytest.raises(ContractError, match='PREPARATION_RUNTIME_FAILED'):
        prepare_source(adapter, req)
    assert len(calls) == 2
    assert adapter.workflow.status(value['workflow_id'])['source_outcomes'] == []


@pytest.mark.parametrize('change', [{'source_sha256':'0' * 64}, {'backend':'other'},
                                  {'schema_version':'other'}])
def test_mismatched_diagnostic_never_becomes_source_failure(tmp_path, monkeypatch, change):
    _, adapter, value, req = setup_worker(tmp_path)
    monkeypatch.setattr('source_preparation.subprocess.run',
                        lambda argv, **kwargs: failed_conversion(argv, change=change))
    with pytest.raises(ContractError, match='SOURCE_IDENTITY_CONFLICT'):
        prepare_source(adapter, req)
    assert adapter.workflow.status(value['workflow_id'])['source_outcomes'] == []


def test_document_exit_without_diagnostic_is_not_proof(tmp_path, monkeypatch):
    _, adapter, value, req = setup_worker(tmp_path)
    monkeypatch.setattr('source_preparation.subprocess.run',
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 4))
    with pytest.raises(ContractError, match='PREPARATION_RUNTIME_FAILED'):
        prepare_source(adapter, req)
    assert adapter.workflow.status(value['workflow_id'])['source_outcomes'] == []


def test_cancelled_conversion_cannot_commit_a_document_failure(tmp_path, monkeypatch):
    from test_p3_knowledge_build import workflow_request
    _, adapter, value, req = setup_worker(tmp_path)
    def convert(argv, **kwargs):
        result = failed_conversion(argv)
        latest = adapter.workflow.status(value['workflow_id'])
        adapter.workflow.cancel(workflow_request(workflow_id=value['workflow_id'],
            actor=latest['actor'], expected_revision=latest['revision']))
        return result
    monkeypatch.setattr('source_preparation.subprocess.run', convert)
    with pytest.raises(ContractError, match='WORKFLOW_STOPPED'):
        prepare_source(adapter, req)
    assert adapter.workflow.status(value['workflow_id'])['source_outcomes'] == []


@pytest.mark.parametrize('code', ['PDF_UNREADABLE', 'CONVERSION_FAILED', 'UNSUPPORTED_FORMAT',
                                  'BUNDLE_VALIDATION_FAILED', 'SOURCE_UNIT_VALIDATION_FAILED'])
def test_operator_repair_can_select_any_typed_failed_source_by_identity(tmp_path, code):
    from test_p3_knowledge_build import workflow_request
    repair_spec = importlib.util.spec_from_file_location('operator_repair', ROOT /
        'hermes-obsidian-governed-ingest-orchestrator/scripts/repair_preparation.py')
    repair = importlib.util.module_from_spec(repair_spec)
    repair_spec.loader.exec_module(repair)
    target, adapter, value, req = setup_worker(tmp_path)
    begun = adapter.worker_begin(req)
    adapter.worker_fail({**req, 'template_hash':begun['template_hash'],
                         'code':code, 'message':'explicit source failure'})
    latest = adapter.workflow.status(value['workflow_id'])
    stopped = adapter.workflow.cancel(workflow_request(workflow_id=value['workflow_id'],
        actor=latest['actor'], expected_revision=latest['revision']))
    evidence = target / 'operator-evidence.json'; evidence.write_text('{}')
    source = stopped['source_outcomes'][0]
    service, _, request = repair.prepare(target, value['workflow_id'],
        [source['content_sha256']], 'generic-repair', ['operator-evidence.json'])
    assert request['reset_sources'][0]['path'] == source['path']
    updated = service.repair_preparation(request)
    assert updated['cancel_requested'] and updated['source_outcomes'] == []
    assert not updated['kanban']['task_map']
    assert (target / updated['repair_history'][-1]['before_ref']).is_file()
    with pytest.raises(ContractError, match='STALE_INPUT'):
        repair.prepare(target, value['workflow_id'], ['0' * 64], 'other', ['operator-evidence.json'])
