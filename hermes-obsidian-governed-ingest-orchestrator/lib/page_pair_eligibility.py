"""Candidate-bound mechanical qualifications; never clear QA or infer independence."""
from hermes_source_units.validation import fingerprint
def inspect(view):
    proof=view.get('evidence_selection',{});evidence=view.get('original_evidence',{})
    reasons=[];missing=[];notes=[]
    if (proof.get('contract')!='page-dependencies/v1' or not proof.get('provenance_valid')
            or proof.get('view_sha256')!=fingerprint(evidence)
            or proof.get('candidates_sha256')!=fingerprint(view.get('candidates',[]))
            or proof.get('proof_sha256')!=fingerprint({k:v for k,v in proof.items() if k!='proof_sha256'})
            or not all(r.get('source_mapping_complete') for r in proof.get('readings',[]))):reasons.append('unproven-current-provenance')
    tasks={t['task']:t for t in evidence.get('tasks',[])}
    if not view.get('candidates'):missing.append('no-assigned-candidates')
    for c in view.get('candidates',[]):
        qa=c.get('evidence_qa',[])
        if not qa or any(q.get('qa')!='usable' for q in qa):reasons.append('assigned-candidate-qa-unresolved')
        notes.extend(q.get('qa_note') for q in qa if q.get('qa_note'))
        if not c.get('source_evidence'):missing.append('candidate-without-source-evidence')
        for link in c.get('source_evidence',[]):
            task=tasks.get(link['reading'],{});materials={m['ref']:m for m in task.get('materials',[])}
            for ref in link.get('support_refs',[]):
                name=ref if isinstance(ref,str) else ref['ref'];m=materials.get(name)
                if m is None:missing.append(link['reading']+'/'+name);continue
                if m.get('text_ref') and m['text_ref'] not in evidence.get('texts',{}):missing.append('text:'+m['text_ref'])
                if m.get('missing') or m.get('unavailable'):missing.append(link['reading']+'/'+name)
            for m in materials.values():
                if m.get('required_dependency') and (m.get('missing') or m.get('unavailable')):missing.append('required:'+m['ref'])
            if task.get('missing'):reasons.append('missing-notice-requires-independent-inspection')
            if any(m.get('asset') or m.get('linked_assets') for m in materials.values()):reasons.append('asset-transport-single')
    if view.get('existing_page'):reasons.append('parent-page-single')
    return {'eligible':not reasons and not missing,'reasons':sorted(set(reasons)),
        'required_missing':sorted(set(missing)),'input_sha256':fingerprint(view),
        'retained_qa_notes':len(notes),'context_retained':True}
def pair(left,right,*,admissions=None,mode=None):
    a,b=left[1],right[1]
    from page_single_admission import enabled,matches
    modern=enabled(a) and enabled(b)
    if mode or modern:
        from page_pair_calls import key
        mode=mode or 'single_admission'
        reasons=[];proofs=[]
        from page_pair_calls import key
        rows=[(admissions or {}).get(key(item[0]),{}).get('qualification',{'required_missing':[],'reasons':['current-single-admission-not-proven']}) for item in (left,right)]
        for item,row in zip((left,right),rows):
            proof=(admissions or {}).get(key(item[0]));proofs.append(proof)
            if not matches(item,proof):reasons.append('current-single-admission-not-proven');continue
            if mode=='qa_only':reasons.extend(r for r in row['reasons'] if r!='assigned-candidate-qa-unresolved')
            elif mode=='single_admission':
                if proof['needs_image_transport']:reasons.append('image-binding-reuse-not-yet-qualified-single')
            else:reasons.append('unknown-pairing-admission-mode')
        reasons=sorted(set(reasons))
        return {'eligible':not reasons and not any(r['required_missing'] for r in rows),'reasons':reasons,
            'pages':rows,'single_admissions':proofs,'scope_binding':'exact independent page inputs; reuse single admission; QA retained without semantic reclassification',
            'publication_eligibility_granted':False}
    rows=[inspect(a),inspect(b)]
    reasons=sorted({r for row in rows for r in row['reasons']})
    from independent_body import enabled
    if not (enabled(a) and enabled(b)) and fingerprint(a.get('original_evidence'))!=fingerprint(b.get('original_evidence')):reasons.append('different-complete-evidence')
    return {'eligible':not reasons and all(r['eligible'] for r in rows),'reasons':reasons,'pages':rows,
        'scope_binding':'independent-page-input-sha256; no textual applicability equality required'}
