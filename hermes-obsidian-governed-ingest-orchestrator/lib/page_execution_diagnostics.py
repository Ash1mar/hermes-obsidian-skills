"""Ordered, mechanical routing reasons; no semantic eligibility inference."""
def page_reasons(page,view,size,*,boundary=False,disposition=None):
    reasons=[]
    if boundary:reasons.append('output_boundary')
    if disposition:reasons.append('explicit_disposition')
    if size>30000:reasons.append('complete_input_oversize')
    if any(m.get('asset') for t in view.get('original_evidence',{}).get('tasks',[]) for m in t.get('materials',[])):
        reasons.append('asset_requires_single')
    if view.get('existing_page'):reasons.append('parent_page_requires_single')
    return reasons

def observe(binding,reason,**fields):
    from hermes_source_units.runtime_telemetry import event
    event('page_route',binding=binding,reason=reason,**fields)
