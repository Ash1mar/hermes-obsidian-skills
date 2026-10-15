"""Read original, authorized evidence for downstream use; never rewrite Passes."""
import copy
import json

from hermes_source_units import ContractError
from hermes_source_units.source_units import _load_json, _vault_path
from hermes_source_units.validation import fingerprint, validate_record


def original_evidence(service, records, actor, registry):
    """Reuse the runtime renderer and immutable package bound to each citation."""
    projections, readings, supplements = [], {}, {}
    for record in {r['pass_id']:r for r in records}.values():
        validate_record('knowledge_pass', record)
        if fingerprint({k:v for k,v in record.items() if k!='pass_id'}) != record['pass_id']:
            raise ContractError('SOURCE_CHANGED', '$', 'citation Pass fingerprint changed')
        key = (record['task_id'], record['reading_package_id'])
        package = _load_json(_vault_path(service.vault,
            f'_system/knowledge-builds/task-{key[0]}/readings/{key[1]}.json'))
        validate_record('reading_package', package)
        service._reading_package_fingerprint(package, key[1])
        if (package['actor'] != actor or package['document_registry_revision'] != registry
                or record['actor'] != actor or record['pass_kind'] != 'citation'
                or record['reading_window_id'] != package['window']['window_id']):
            raise ContractError('STALE_INPUT', '$', 'citation reading authority changed')
        from hermes_source_units.reading_dependencies import required_reading, citation_dependency_gaps
        if key not in supplements:
            _, supplements[key] = required_reading(service.source, package)
        supplement = supplements[key]
        gaps = citation_dependency_gaps(service.source, package, record, supplement)
        if gaps:
            raise ContractError('INCOMPLETE_COVERAGE', '$', json.dumps({
                'reason':'required original dependency lacks citation inspection','gaps':gaps},ensure_ascii=False))
        if key in readings:
            continue
        projection = service.model_input(key[0], package, persist=False)
        # Full original core/context is retained so short findings cannot hide
        # scope or omissions. No semantic information is inferred for old records.
        projection['view']['continuation'] = {}
        projections.append(projection)
        readings[key] = {'reading':'t'+str(len(projections)), 'mapping':projection['mapping']}
    packet = service.model_packet(projections, 'downstream-original-evidence', persist=False)['view']
    for task in packet['tasks']:
        for field in ('input_id', 'next_sequence', 'continuation'):
            task.pop(field, None)
    return packet, readings


def support(service, record, candidate, readings):
    observed = readings[(record['task_id'], record['reading_package_id'])]
    return {'reading':observed['reading'], 'support_refs':[
        service._model_handle(ref, observed['mapping']) for ref in candidate['support_refs']]}


def selected(packet, readings):
    """Select complete original packages, dropping only unrelated shared pools."""
    tasks = [copy.deepcopy(t) for t in packet['tasks'] if t['task'] in readings]
    materials = [m for t in tasks for m in t['materials']]
    texts = {m['text_ref'] for m in materials if m.get('text_ref')}
    qa = {m['qa_ref'] for m in materials if m.get('qa_ref')}
    headings = {m['heading'] for m in materials}
    result = {'tasks':tasks, 'texts':{k:v for k,v in packet['texts'].items() if k in texts},
        'headings':{str(i):packet['headings'][i] for i in sorted(headings)}}
    if qa: result['qa'] = {k:v for k,v in packet['qa'].items() if k in qa}
    if packet.get('qa_rule'): result['qa_rule'] = packet['qa_rule']
    return result
