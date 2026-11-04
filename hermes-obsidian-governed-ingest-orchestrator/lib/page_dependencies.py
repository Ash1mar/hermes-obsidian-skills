"""Pure, version-bound page dependency proof. Never infer semantic independence."""
import copy
from hermes_source_units import ContractError
from hermes_source_units.validation import fingerprint

CONTRACT='page-dependencies/v1'

def provenance(packet,readings):
    return {'contract':CONTRACT,'packet_sha256':fingerprint(packet),'readings':{
        row['reading']:{'task_id':key[0],'reading_package_id':key[1],
                        'mapping':copy.deepcopy(row['mapping'])} for key,row in readings.items()}}

def derive(packet,candidates,proof):
    original=fingerprint(packet);valid=(isinstance(proof,dict) and proof.get('contract')==CONTRACT
                                       and proof.get('packet_sha256')==original)
    result=copy.deepcopy(packet);rows=[];support={};missing=[]
    for index,c in enumerate(candidates):
        for link in c.get('source_evidence',[]):
            reading=link['reading'];support.setdefault(reading,[]).extend(copy.deepcopy(link['support_refs']))
    tasks={t['task']:t for t in packet['tasks']}
    for reading,refs in support.items():
        known={m['ref'] for m in tasks.get(reading,{}).get('materials',[])}
        if any((r if isinstance(r,str) else r['ref']) not in known for r in refs):missing.append(reading)
    if missing:raise ContractError('UNRESOLVED_REFERENCE','$','support outside reading: '+','.join(missing))
    for task in result['tasks']:
        reading=task['task'];p=(proof or {}).get('readings',{}).get(reading,{})
        mapping=p.get('mapping',{});wanted={r if isinstance(r,str) else r['ref'] for r in support.get(reading,[])}
        complete=valid and all(m['ref'] in mapping for m in task['materials'])
        retained=[];equivalents={};decisions=[]
        # Referenced and required rows come first, so no needed alias is removed.
        for m in sorted(task['materials'],key=lambda m:m['ref'] not in wanted and not m.get('required_dependency')):
            ref=m['ref'];source=mapping.get(ref)
            payload=copy.deepcopy(m);payload.pop('ref',None)
            if m.get('text_ref'):payload['resolved_text']=packet.get('texts',{}).get(m['text_ref'])
            if m.get('qa_ref'):payload['resolved_qa']=packet.get('qa',{}).get(m['qa_ref'])
            heading=m.get('heading');hs=packet.get('headings',{})
            payload['heading_path']=hs.get(str(heading),hs.get(heading)) if isinstance(hs,dict) else hs[heading] if isinstance(heading,int) and heading<len(hs) else None
            signature=fingerprint({'source':source,'payload':payload})
            prior=equivalents.get(signature)
            exclude=complete and prior is not None and ref not in wanted and not m.get('required_dependency')
            reason=('exact_same_source_span_and_all_context_qa_asset_fields' if exclude else
                    'exact_support' if ref in wanted else 'registered_required_asset' if m.get('required_dependency') else
                    'context_boundary_unproven_full_material_retained')
            decisions.append({'ref':ref,'decision':'exclude' if exclude else 'retain','reason':reason,
                'equivalent_retained_ref':prior if exclude else None,'canonical_source':copy.deepcopy(source),
                'material_sha256':fingerprint(m),'heading_path':payload['heading_path'],
                'qa':copy.deepcopy(packet.get('qa',{}).get(m.get('qa_ref'))),
                'required_dependency':bool(m.get('required_dependency')),'assets':copy.deepcopy(m.get('asset'))})
            if not exclude:retained.append(m);equivalents[signature]=ref
        keep={m['ref'] for m in retained};task['materials']=[m for m in task['materials'] if m['ref'] in keep]
        rows.append({'reading':reading,'reading_package_id':p.get('reading_package_id'),'source_mapping_complete':complete,
                     'exact_support':support.get(reading,[]),'context_closed':False,
                     'fallback':'full_context' if not any(d['decision']=='exclude' for d in decisions) else 'lossless_source_equivalence_only',
                     'decisions':decisions})
    # All headings, text pools, QA and truncation/missing notices remain untouched.
    manifest={'contract':CONTRACT,'provenance_valid':valid,'original_sha256':original,'candidates_sha256':fingerprint(candidates),
              'candidate_scope':copy.deepcopy(candidates),'provenance_sha256':fingerprint(proof),
              'readings':rows,'view_sha256':fingerprint(result)}
    manifest['proof_sha256']=fingerprint(manifest)
    return result,manifest
