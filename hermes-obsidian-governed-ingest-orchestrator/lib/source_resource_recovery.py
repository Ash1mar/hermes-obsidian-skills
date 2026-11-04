"""One durable resource retry per source input; never a document-quality retry."""
import hashlib
import json
from hermes_source_units import ContractError
from hermes_source_units.source_units import _json_bytes, _write_atomic
from hermes_source_units.runtime_telemetry import event, span


def recover(candidate, root, source, backend, invoke, check, *, allowed):
    """Caller holds source preparation lock; invoke is supervised and synchronous."""
    diagnostic=candidate/'conversion-result.json'
    if not allowed or not diagnostic.is_file():return candidate
    row=json.loads(diagnostic.read_text(encoding='utf-8'))
    if (row.get('schema_version')!='hermes-conversion-result/v1' or
            row.get('source_sha256')!=source['content_sha256'] or row.get('backend')!=backend):
        raise ContractError('SOURCE_IDENTITY_CONFLICT','$','resource failure identity changed')
    if (row.get('status')!='runtime_failed' or row.get('resource_failure')!='cuda_out_of_memory'
            or type(row.get('engine_exit')) is not int or row['engine_exit']<=0
            or (candidate/'manifest.json').exists()):return candidate
    journal=root/'resource-retry.json';retry=candidate.with_name(candidate.name+'-resource-retry')
    identity={'contract':'source-resource-retry/v1','source_sha256':source['content_sha256'],
        'backend':backend,'original':str(candidate),'retry':str(retry),
        'diagnostic_sha256':hashlib.sha256(diagnostic.read_bytes()).hexdigest(),'additional_attempts':1}
    if journal.exists():
        saved=json.loads(journal.read_text(encoding='utf-8'))
        if saved['identity']!=identity:
            return candidate  # Shared source budget consumed by another backend.
        if (retry/'conversion-result.json').is_file():return retry
        raise ContractError('PREPARATION_RUNTIME_FAILED','$','reserved resource retry has unknown completion; no repeat')
    if retry.exists():raise ContractError('PREPARATION_RUNTIME_FAILED','$','unowned resource retry output exists')
    check()
    _write_atomic(journal,_json_bytes({'identity':identity,'state':'reserved'}))
    event('source_resource_retry_reserved',source_sha256=source['content_sha256'],backend=backend,additional_attempts=1)
    with span('source_resource_retry',source_sha256=source['content_sha256'],backend=backend) as trace:
        result=invoke(retry,root/(candidate.name+'-resource-retry.log'))
        trace['returncode']=result.returncode
    check()
    _write_atomic(journal,_json_bytes({'identity':identity,'state':'ended','returncode':result.returncode}))
    return retry
