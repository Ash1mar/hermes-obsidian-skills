"""Bound, deterministic source preparation using the controlled-ingest APIs."""
from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time

from hermes_source_units import ContractError, FileSourceUnitService
from hermes_source_units.source_units import _exclusive_lock, _vault_path, _write_atomic, _json_bytes, UNIT_ROOT
from hermes_source_units.ingest_workflow import WORKFLOW_ROOT
from hermes_source_units.validation import fingerprint
from hermes_source_units.workflow_guard import worker_binding, workflow_write_guard


def _fail(code, message):
    raise ContractError(code, "$", message)


def prepare_source(adapter, request):
    """One card only; never plan a batch, dispatch successors, or approve quality."""
    if not str(request["node"]).startswith("source-prepare:"):
        _fail("ACCESS_DENIED", "source preparation requires a source card")
    begun = adapter.worker_begin(request)
    bound = {**request, "template_hash": begun["template_hash"]}
    source, actor = begun["assigned_source"], begun["actor"]
    vault = adapter.workflow.vault
    scripts = Path(__file__).resolve().parents[2] / "hermes-obsidian-controlled-ingest/scripts"
    sys.path.insert(0, str(scripts))
    from governance_repository import JsonGovernanceRepository, GovernanceError
    from manage_document_governance import command_ingest_finish
    from validate_document_bundle import validate_bundle

    attempt_identity = fingerprint({'node': request['node'],
                                    'input_fingerprint': begun['input_fingerprint']})[:16]
    root = _vault_path(vault, f"{WORKFLOW_ROOT}/{request['workflow_id']}/source-preparation/{attempt_identity}")
    with _exclusive_lock(root / ".prepare.lock"), worker_binding(bound):
        adapter.worker_check(bound)
        identity = adapter.worker_register_source(bound)
        service = FileSourceUnitService(vault)
        resource = identity["resource_id"]
        current = service._current(resource)
        # Resuming this card can reuse an already published, validated current set.
        if current:
            checked = service.validate(resource, current["unit_set_id"])
            manifest, units, _ = service._load_set(resource, current["unit_set_id"])
            if (manifest["identity"]["document_id"] != identity["document_id"] or
                    manifest["identity"]["version_id"] != identity["version_id"] or
                    not units or any(unit["source_sha256"] != source["content_sha256"] for unit in units)):
                _fail("SOURCE_IDENTITY_CONFLICT", "current UnitSet differs from assigned identity")
            refs = [f"{UNIT_ROOT}/{resource}/{current['unit_set_id']}/manifest.json"]
            return _complete(adapter, bound, resource, checked["unit_set_id"], refs, reused=True)

        # A supplied Bundle is explicit resumption; automatic reuse is restricted
        # to this workflow/source's deterministic attempt directories.
        bundle_ref = request.get("bundle") or _repaired_bundle(adapter, request, source)
        if bundle_ref:
            bundle = _vault_path(vault, str(bundle_ref))
            if not any(bundle.is_relative_to(_vault_path(vault, location))
                       for location in ("10_Raw/converted", "_system/reports/source-bundles")):
                _fail("UNSAFE_PATH", "Bundle must be a governed derived Bundle")
            _validate_identity(bundle, source)
        else:
            raw = _vault_path(vault, source["path"])
            if raw.suffix.lower() != ".pdf":
                _fail("PREPARATION_REVIEW_REQUIRED", "automatic preparation currently supports PDF; use governed manual preparation for other formats")
            generation = fingerprint({'workflow_id': request['workflow_id'],
                                      'input_fingerprint': begun['input_fingerprint']})[:16]
            base = f"_system/reports/source-bundles/{raw.stem}-{source['content_sha256'][:16]}-{generation}"
            bundle = None
            document_failures = []
            with tempfile.TemporaryDirectory(prefix="hermes-source-binding-") as temporary:
                binding_file = Path(temporary) / "binding.json"
                binding_file.write_text(json.dumps(bound, ensure_ascii=False), encoding="utf-8")
                for attempt, backend in ((1, "hybrid-engine"), (2, "pipeline")):
                    adapter.worker_check(bound)
                    candidate = _vault_path(vault, f"{base}/attempt-{attempt}")
                    result = None
                    if not candidate.exists():
                        root.mkdir(parents=True, exist_ok=True)
                        with (root / f"conversion-{attempt}.log").open("wb") as output:
                            result = subprocess.run(
                                [sys.executable, str(scripts / "convert_pdf_with_mineru_bundle.py"),
                                 str(raw), "-o", str(candidate), "--vault", str(vault),
                                 "--worker-binding", str(binding_file), "--backend", backend,
                                 "--model-source", "local"], stdout=output, stderr=subprocess.STDOUT,
                                check=False)
                        # Missing entrypoints, binding or environment errors cannot
                        # be repaired by parsing the same PDF with weaker settings.
                    adapter.worker_check(bound)
                    if result is not None and result.returncode not in (0, 3, 4):
                        _fail("PREPARATION_RUNTIME_FAILED", f"converter rejected attempt {attempt}; inspect {root.relative_to(vault)}; source outcome remains pending")
                    diagnostic_path = candidate / 'conversion-result.json'
                    diagnostic = json.loads(diagnostic_path.read_text(encoding='utf-8')) if diagnostic_path.is_file() else None
                    if diagnostic is not None:
                        if (diagnostic.get('schema_version') != 'hermes-conversion-result/v1'
                                or diagnostic.get('source_sha256') != source['content_sha256']
                                or diagnostic.get('backend') != backend):
                            _fail('SOURCE_IDENTITY_CONFLICT', 'conversion diagnostic differs from assigned attempt')
                        if diagnostic.get('status') == 'document_failed' and diagnostic.get('error_code') == 'CONVERSION_FAILED':
                            if (candidate / 'manifest.json').exists():
                                _fail('PREPARATION_RUNTIME_FAILED', 'conversion failure conflicts with Bundle manifest')
                            document_failures.append(diagnostic_path.relative_to(vault).as_posix())
                            continue
                        if diagnostic.get('status') != 'converted':
                            _fail('PREPARATION_RUNTIME_FAILED', f'converter execution blocked; inspect {diagnostic_path.relative_to(vault)}; source outcome remains pending')
                    if result is not None and result.returncode == 4:
                        _fail('PREPARATION_RUNTIME_FAILED', 'document exit lacks a bound document failure diagnostic')
                    if not (candidate / 'manifest.json').is_file():
                        _fail('PREPARATION_RUNTIME_FAILED', f'converter produced no verified Bundle or document failure on attempt {attempt}; inspect {root.relative_to(vault)}; source outcome remains pending')
                    if (candidate / "manifest.json").is_file():
                        _validate_identity(candidate, source)
                    validation = validate_bundle(candidate)
                    _write_atomic(root / f"validation-{attempt}.json", _json_bytes(validation))
                    if validation["status"] != "fail":
                        bundle = candidate
                        break
            if bundle is None:
                if len(document_failures) == 2:
                    evidence = document_failures + [(root / f'conversion-{i}.log').relative_to(vault).as_posix() for i in (1, 2)]
                    for retry in range(16):
                        adapter.worker_check(bound)
                        try:
                            return adapter.worker_fail({**bound, 'code': 'CONVERSION_FAILED',
                                'message': 'Both bounded conversion backends reported typed document load failures',
                                'artifact_refs': evidence})
                        except ContractError as exc:
                            if exc.code not in ('REVISION_CONFLICT', 'LOCK_BUSY') or retry == 15:
                                raise
                            time.sleep(0.05)
                _fail("PREPARATION_REVIEW_REQUIRED", f"two bounded conversion attempts lack a valid Bundle; inspect {root.relative_to(vault)}; source outcome remains pending")

        adapter.worker_check(bound)
        _validate_identity(bundle, source)
        validation = validate_bundle(bundle)
        _write_atomic(root / "validation.json", _json_bytes(validation))
        if validation["status"] == "fail":
            _fail("PREPARATION_REVIEW_REQUIRED", "Bundle failed validation; inspect source-preparation validation.json")
        repository = JsonGovernanceRepository(vault)
        try:
            record, revision = repository.get_version(identity['version_id'])
            governance = json.loads((bundle/'manifest.json').read_text(encoding='utf-8-sig')).get('governance', {})
            finished = (record.get('processing_status') == 'completed' and
                all(governance.get(key) == identity[key] for key in ('resource_id','document_id','version_id'))
                and governance.get('vault_id') == service.vault_id)
            if not finished:
                # ingest-finish annotates Bundle manifests; source material is read-only.
                if bundle.is_relative_to(_vault_path(vault, '10_Raw')):
                    derived = _vault_path(vault, f"_system/reports/source-bundles/reused-{source['content_sha256']}")
                    if not derived.exists():
                        with workflow_write_guard(vault, actor=actor):
                            shutil.copytree(bundle, derived)
                    bundle = derived
                    _validate_identity(bundle, source)
                with workflow_write_guard(vault, actor=actor):
                    command_ingest_finish(repository, Namespace(bundle=bundle,
                        version_id=identity["version_id"], expected_revision=revision, actor=actor))
        except GovernanceError as exc:
            _fail("SOURCE_REGISTRATION_BLOCKED", str(exc))
        adapter.worker_check(bound)
        prepared = service.prepare_bundle(bundle.relative_to(vault).as_posix())
        current = service._current(resource)
        built = service.build({"artifact_manifest": prepared["artifact_manifest"],
            "config": service.config, "actor": actor,
            "expected_revision": current["revision"] if current else 0})
        checked = service.validate(resource, built["manifest"]["unit_set_id"])
        refs = [prepared["artifact_manifest"],
                (bundle / "manifest.json").relative_to(vault).as_posix(),
                (root / "validation.json").relative_to(vault).as_posix()]
        return _complete(adapter, bound, resource, checked["unit_set_id"], refs,
                         quality=validation["status"])


def _validate_identity(bundle, source):
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8-sig"))
    if manifest.get("source", {}).get("sha256") != source["content_sha256"]:
        _fail("SOURCE_IDENTITY_CONFLICT", "Bundle does not carry assigned raw SHA-256")


def _repaired_bundle(adapter, request, source):
    """Reuse only this source's explicit QA Bundle in a verified repair snapshot."""
    import hashlib
    workflow = adapter.workflow.status(request['workflow_id'])
    for repair in reversed(workflow.get('repair_history', [])):
        path = _vault_path(adapter.workflow.vault, repair['before_ref'])
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != repair['before_sha256']:
            _fail('SOURCE_CHANGED', 'repair snapshot hash differs')
        previous = json.loads(payload)
        for item in previous.get('source_outcomes', []):
            if (item['path'] != source['path'] or item['content_sha256'] != source['content_sha256']
                    or item.get('error_code') != 'SOURCE_UNIT_VALIDATION_FAILED'):
                continue
            for ref in item.get('artifact_refs', []):
                if str(ref).endswith('/manifest.json'):
                    bundle = _vault_path(adapter.workflow.vault, ref).parent
                    _validate_identity(bundle, source)
                    return bundle.relative_to(adapter.workflow.vault).as_posix()
    return None


def _complete(adapter, bound, resource, unit_set, refs, *, reused=False, quality=None):
    for attempt in range(16):
        adapter.worker_check(bound)
        try:
            result = adapter.worker_complete({**bound, "resource_id": resource,
                "unit_set_id": unit_set, "artifact_refs": refs})
            return {**result, "resource_id": resource, "unit_set_id": unit_set,
                    "reused_current": reused, "quality": quality,
                    "artifact_refs": refs}
        except ContractError as exc:
            if exc.code not in ("REVISION_CONFLICT", "LOCK_BUSY") or attempt == 15:
                raise
            time.sleep(0.05)
