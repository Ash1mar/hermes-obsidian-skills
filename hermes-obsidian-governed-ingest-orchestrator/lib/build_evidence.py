"""Common reference/material checks; never semantic approval or OCR attribution."""
from hermes_source_units.validation import fingerprint


def page_check(view):
    packet=view.get('original_evidence',{});tasks={t['task']:t for t in packet.get('tasks',[])}
    failures=[];support_count=asset_count=required_count=0
    for task in tasks.values():
        materials=task.get('materials',[])
        if not isinstance(materials,list):
            failures.append('material table must be checked before wire encoding');continue
        if len({m['ref'] for m in materials})!=len(materials):failures.append('duplicate material handle')
        for material in materials:
            asset_count+=bool(material.get('asset'));required_count+=bool(material.get('required_dependency'))
            ref=material.get('text_ref')
            if ref and ref not in packet.get('texts',{}):failures.append('missing text pool entry')
            qa=material.get('qa_ref')
            if qa and qa not in packet.get('qa',{}):failures.append('missing QA pool entry')
    for candidate in view.get('citation_evidence',[]):
        for source in candidate.get('source_evidence',[]):
            task=tasks.get(source['reading'])
            if task is None:failures.append('candidate reading not delivered');continue
            offered={m['ref']:m for m in task['materials']}
            for ref in source['support_refs']:
                support_count+=1
                handle=ref.get('ref') if isinstance(ref,dict) else ref
                if handle not in offered:
                    failures.append('candidate support handle not delivered');continue
                if isinstance(ref,dict):
                    span=ref.get('span');material=offered[handle]
                    source_span=material.get('source_span')
                    if source_span is None and material.get('text_ref'):
                        source_span={'start':0,'end':len(packet['texts'][material['text_ref']])}
                    if not span or not source_span or not source_span['start']<=span['start']<span['end']<=source_span['end']:
                        failures.append('candidate subspan outside delivered material')
    return {'ok':not failures,'failures':failures,'support_handles_checked':support_count,
        'registered_assets_in_packet':asset_count,'explicit_required_materials':required_count,
        'packet_sha256':fingerprint(packet),
        'authority':'current hash-valid source renderer, required-reading supplements and citation inspections checked by snapshot assembly; exact consumed native wires authenticated by finite executor',
        'coverage_rule':'assigned candidate ownership and authoritative run coverage; context cropping is not a planned coverage exclusion'}


def check(value):
    pages={v['page']:page_check(v) for v in value['views']}
    return {'contract':'build-common-evidence-checks/v1','ok':all(v['ok'] for v in pages.values()),
        'pages':pages,'page_count':len(pages),'run_coverage_checked':True,
        'semantic_attribution_inferred':False}
